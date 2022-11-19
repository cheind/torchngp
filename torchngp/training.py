import copy
import dataclasses
import logging
import time
from typing import Union, Optional
from itertools import islice

import torch
import torch.nn
import torch.nn.functional as F
import torch.utils.data
from PIL import Image
from tqdm import tqdm
from pathlib import Path

from . import geometric, rendering, sampling, scenes, volumes, plotting, radiance

_logger = logging.getLogger("torchngp")


class MultiViewDataset(torch.utils.data.IterableDataset):
    def __init__(
        self,
        camera: geometric.MultiViewCamera,
        images: torch.Tensor,
        n_rays_per_view: Optional[int] = None,
        random: bool = True,
        subpixel: bool = True,
    ):
        self.camera = camera
        self.images = images
        self.n_pixels_per_cam = camera.size.prod().item()
        if n_rays_per_view is None:
            # width of image per mini-batch
            n_rays_per_view = camera.size[0].item()  # type: ignore
        self.n_rays_per_view = n_rays_per_view
        self.random = random
        self.subpixel = subpixel if random else False

    def __iter__(self):
        if self.random:
            return islice(
                sampling.generate_random_uv_samples(
                    camera=self.camera,
                    image=self.images,
                    n_samples_per_cam=self.n_rays_per_view,
                    subpixel=self.subpixel,
                ),
                len(self),
            )
        else:
            return sampling.generate_sequential_uv_samples(
                camera=self.camera,
                image=self.images,
                n_samples_per_cam=self.n_rays_per_view,
                n_passes=1,
            )

    def __len__(self) -> int:
        # Number of mini-batches required to match with number of total pixels
        return self.n_pixels_per_cam // self.n_rays_per_view


def create_fwd_bwd_closure(
    vol: volumes.Volume,
    renderer: rendering.RadianceRenderer,
    tsampler: sampling.RayStepSampler,
    scaler: torch.cuda.amp.GradScaler,
    n_acc_steps: int,
):
    # https://pytorch.org/docs/stable/notes/amp_examples.html#gradient-accumulation
    def run_fwd_bwd(
        cam: geometric.MultiViewCamera, uv: torch.Tensor, rgba: torch.Tensor
    ):
        B, N, M, C = rgba.shape
        maps = {"color", "alpha"}

        with torch.cuda.amp.autocast(enabled=scaler.is_enabled()):
            uv = uv.permute(1, 0, 2, 3).reshape(N, B * M, 2)
            rgba = rgba.permute(1, 0, 2, 3).reshape(N, B * M, C)
            rgb, alpha = rgba[..., :3], rgba[..., 3:4]
            noise = torch.empty_like(rgb).uniform_(0.0, 1.0)
            # Dynamic noise background with alpha composition
            # Encourages the model to learn zero density in empty regions
            # Dynamic background is also combined with prediced colors, so
            # model does not have to learn randomness.
            gt_rgb_mixed = rgb * alpha + noise * (1 - alpha)

            # Predict
            pred_maps = renderer.trace_uv(vol, cam, uv, tsampler, which_maps=maps)
            pred_rgb, pred_alpha = pred_maps["color"], pred_maps["alpha"]
            # Mix
            pred_rgb_mixed = pred_rgb * pred_alpha + noise * (1 - pred_alpha)

            # Loss normalized by number of accumulation steps before
            # update
            loss = F.smooth_l1_loss(pred_rgb_mixed, gt_rgb_mixed)
            loss = loss / n_acc_steps

        # Scale the loss
        scaler.scale(loss).backward()
        return loss

    return run_fwd_bwd


@torch.no_grad()
def render_images(
    vol: volumes.Volume,
    renderer: rendering.RadianceRenderer,
    cam: geometric.MultiViewCamera,
    tsampler: sampling.RayStepSampler,
    use_amp: bool,
    n_samples_per_view: int,
) -> tuple[torch.Tensor, torch.Tensor]:
    with torch.cuda.amp.autocast(enabled=use_amp):
        maps = renderer.trace_maps(
            vol, cam, tsampler=tsampler, n_samples_per_cam=n_samples_per_view
        )
        pred_rgba = torch.cat((maps["color"], maps["alpha"]), -1).permute(0, 3, 1, 2)
    return pred_rgba


def save_image(fname: Union[str, Path], rgba: torch.Tensor):
    grid_img = (
        (plotting.make_image_grid(rgba, checkerboard_bg=False, scale=1.0) * 255)
        .to(torch.uint8)
        .permute(1, 2, 0)
        .cpu()
        .numpy()
    )
    Image.fromarray(grid_img, mode="RGBA").save(fname)


@dataclasses.dataclass
class OptimizerParams:
    lr: float = 1e-2
    betas: tuple[float, float] = (0.9, 0.99)
    eps: float = 1e-15
    decay_encoder: float = 0.0
    decay_density: float = 1e-6
    decay_color: float = 1e-6
    sched_factor: float = 0.75
    sched_patience: int = 20
    sched_minlr: float = 1e-4


@dataclasses.dataclass
class NeRFTrainer:
    output_dir: Path = Path("./tmp")
    train_cam_idx: int = 0
    train_slice: Optional[str] = None
    train_max_time: float = 60 * 10
    train_max_epochs: int = 3
    val_cam_idx: int = -1
    val_slice: Optional[str] = ":3"
    n_rays_batch: int = 2**14
    n_rays_minibatch: int = 2**14
    val_n_rays: int = int(1e6)
    val_min_loss: float = 5e-3
    n_worker: int = 4
    use_amp: bool = True
    random_uv: bool = True
    subpixel_uv: bool = True
    preload: bool = False
    optimizer: OptimizerParams = dataclasses.field(default_factory=OptimizerParams)
    dev: Optional[torch.device] = None

    def __post_init__(self):
        if self.dev is None:
            self.dev = (
                torch.device("cuda")
                if torch.cuda.is_available()
                else torch.device("cpu")
            )
        self.dev = self.dev
        _logger.info(f"Using device {self.dev}")
        _logger.info(f"Output directory set to {self.output_dir.as_posix()}")

    def train(
        self,
        scene: scenes.Scene,
        volume: volumes.Volume,
        renderer: Optional[rendering.RadianceRenderer] = None,
        tsampler: Optional[sampling.RayStepSampler] = None,
    ):
        self.scene = scene
        self.volume = volume
        self.renderer = renderer or rendering.RadianceRenderer(ray_extension=1.0)
        self.tsampler = tsampler or sampling.StratifiedRayStepSampler(n_samples=128)

        # Move all relevent modules to device
        self._move_mods_to_device()

        # Locate the cameras to be used for training/validating
        train_cam, val_cam = self._find_cameras()

        # Bookkeeping
        n_acc_steps = self.n_rays_batch // self.n_rays_minibatch
        n_rays_per_view = int(self.n_rays_minibatch / train_cam.n_views / self.n_worker)
        val_interval = max(int(self.val_n_rays / self.n_rays_batch), 1)

        # Train dataloader
        train_dl = self._create_train_dataloader(
            train_cam, n_rays_per_view, self.n_worker
        )

        # Create optimizers, schedulers
        opt, sched = self._create_optimizers()

        # Setup AMP
        scaler = torch.cuda.amp.GradScaler(enabled=self.use_amp)

        # Setup closure for gradient accumulation
        fwd_bwd_fn = create_fwd_bwd_closure(
            self.volume, self.renderer, self.tsampler, scaler, n_acc_steps=n_acc_steps
        )

        # Enter main loop
        pbar_postfix = {"loss": 0.0}
        self.global_step = 0
        loss_acc = 0.0
        t_started = time.time()
        t_val_elapsed = 0.0
        for _ in range(self.train_max_epochs):
            pbar = tqdm(train_dl, mininterval=0.1, leave=False)
            for uv, rgba in pbar:
                if (time.time() - t_started - t_val_elapsed) > self.train_max_time:
                    _logger.info("Max training time elapsed.")
                    return
                uv = uv.to(self.dev)
                rgba = rgba.to(self.dev)
                loss = fwd_bwd_fn(train_cam, uv, rgba)
                loss_acc += loss.item()

                if (self.global_step + 1) % n_acc_steps == 0:
                    scaler.step(opt)
                    scaler.update()
                    sched.step(loss)
                    opt.zero_grad(set_to_none=True)
                    self.volume.spatial_filter.update(
                        self.volume.radiance_field,
                        global_step=self.global_step,
                    )

                    pbar_postfix["loss"] = loss_acc
                    pbar_postfix["lr"] = sched._last_lr[0]  # type: ignore
                    loss_acc = 0.0

                if ((self.global_step + 1) % val_interval == 0) and (
                    pbar_postfix["loss"] <= self.val_min_loss
                ):
                    t_val_start = time.time()
                    # TODO: consider number of rays, gpu not utilized! also instead of
                    # stack copy image, parts in trace_maps?
                    self.validation_step(
                        val_camera=val_cam, n_rays_per_view=self.n_rays_minibatch
                    )
                    t_val_elapsed += time.time() - t_val_start

                pbar.set_postfix(**pbar_postfix, refresh=False)
                self.global_step += 1

    @torch.no_grad()
    def validation_step(
        self, val_camera: geometric.MultiViewCamera, n_rays_per_view: int
    ):
        val_rgba = render_images(
            self.volume,
            self.renderer,
            val_camera,
            self.tsampler,
            self.use_amp,
            n_samples_per_view=n_rays_per_view,
        )
        save_image(self.output_dir / f"img_val_step={self.global_step}.png", val_rgba)
        # TODO:this is a different loss than in training
        # val_loss = F.mse_loss(val_rgba[:, :3], val_scene.images.to(dev)[:, :3])
        # pbar_postfix["val_loss"] = val_loss.item()

    def _create_train_dataloader(
        self, train_cam: geometric.MultiViewCamera, n_rays_per_view: int, n_worker: int
    ):
        if not self.preload:
            train_cam = copy.deepcopy(train_cam).cpu()

        train_ds = MultiViewDataset(
            camera=train_cam,
            images=train_cam.load_images(),
            n_rays_per_view=n_rays_per_view,
            random=self.random_uv,
            subpixel=self.subpixel_uv,
        )

        train_dl = torch.utils.data.DataLoader(
            train_ds,
            batch_size=n_worker,
            num_workers=n_worker,
        )
        return train_dl

    def _move_mods_to_device(self):
        for m in [self.scene, self.volume, self.renderer, self.tsampler]:
            m.to(self.dev)  # changes self.scene to be on device as well?!

    def _find_cameras(
        self,
    ) -> tuple[geometric.MultiViewCamera, geometric.MultiViewCamera]:
        train_cam: geometric.MultiViewCamera = self.scene.cameras[self.train_cam_idx]
        val_cam: geometric.MultiViewCamera = self.scene.cameras[self.val_cam_idx]
        if self.train_slice is not None:
            train_cam = train_cam[_string_to_slice(self.train_slice)]
        if self.val_slice is not None:
            val_cam = val_cam[_string_to_slice(self.val_slice)]
        return train_cam, val_cam

    def _create_optimizers(
        self,
    ) -> tuple[torch.optim.Optimizer, torch.optim.lr_scheduler._LRScheduler]:
        nerf: radiance.NeRF = self.volume.radiance_field  # type: ignore
        opt = torch.optim.AdamW(
            [
                {
                    "params": nerf.pos_encoder.parameters(),
                    "weight_decay": self.optimizer.decay_encoder,
                },
                {
                    "params": nerf.density_mlp.parameters(),
                    "weight_decay": self.optimizer.decay_density,
                },
                {
                    "params": nerf.color_mlp.parameters(),
                    "weight_decay": self.optimizer.decay_color,
                },
            ],
            betas=self.optimizer.betas,
            eps=self.optimizer.eps,
            lr=self.optimizer.lr,
        )

        sched = torch.optim.lr_scheduler.ReduceLROnPlateau(
            opt,
            mode="min",
            factor=self.optimizer.sched_factor,
            patience=self.optimizer.sched_patience,
            min_lr=self.optimizer.sched_minlr,
        )

        return opt, sched


def _string_to_slice(sstr):
    # https://stackoverflow.com/questions/43089907/using-a-string-to-define-numpy-array-slice
    return tuple(
        slice(*(int(i) if i else None for i in part.strip().split(":")))
        for part in sstr.strip("[]").split(",")
    )
