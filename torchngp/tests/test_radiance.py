from typing import Optional
import torch
from torch.testing import assert_close
import matplotlib as mpl

from torchngp import geometric, radiance


class ColorGradientRadianceField(radiance.RadianceField):
    """A test radiance field with a color gradient in x-dir and a planar surface."""

    def __init__(
        self,
        surface_pos: float = 0.2,  # x-pos in ndc [-1,1]
        surface_dim: int = 0,  # if zero, plane normal parallel to x
        density_scale: float = 1.0,
        cmap: str = "gray",
    ):
        self.cmap = mpl.colormaps[cmap]
        self.surface_dim = surface_dim
        self.density_scale = density_scale
        self.surface_pos = (surface_pos + 1.0) * 0.5
        self.n_color_cond_dims = 0  # unsupported
        self.n_color_dims = 3
        self.n_density_dims = 1

    def encode(self, xyz: torch.Tensor) -> torch.Tensor:
        return (xyz + 1.0) * 0.5  # use [0,1] here to match colormap

    def decode_density(self, f: torch.Tensor) -> torch.Tensor:
        nxyz = f
        density = nxyz[..., self.surface_dim : self.surface_dim + 1] - self.surface_pos

        mask = density < 0
        density[mask] = 0.0
        density[~mask] *= self.density_scale

        return density

    def decode_color(
        self, f: torch.Tensor, cond: Optional[torch.Tensor] = None
    ) -> torch.Tensor:
        del cond
        nxyz = f

        # Colors (N,...,3)
        colors = self.cmap(nxyz[..., self.surface_dim].cpu().numpy())
        colors = torch.tensor(colors[..., :3])
        colors = colors.to(nxyz.dtype)

        return colors

    def __call__(
        self, nxyz: torch.Tensor, color_cond: Optional[torch.Tensor] = None
    ) -> tuple[torch.Tensor, torch.Tensor]:

        f = self.encode(nxyz)
        density = self.decode_density(f)
        color = self.decode_color(f)
        return color, density


def test_radiance_integrate_path():
    o = torch.tensor([[0.0, 0.0, 0.0]])
    d = torch.tensor([[1.0, 0.0, 0.0]])

    ts = torch.linspace(0, 1, 100).view(100, 1, 1)
    xyz = geometric.evaluate_ray(o, d, ts)  # (100,1,3)

    aabb = torch.Tensor([[0.0] * 3, [1.0] * 3])
    nxyz = geometric.convert_world_to_box_normalized(xyz, aabb)

    rf = ColorGradientRadianceField(
        surface_pos=0.2 * 2 - 1,
        density_scale=float("inf"),  # hard surface boundary
        cmap="gray",
    )

    ts_padded = torch.cat((ts, torch.tensor(1.0).view(1, 1, 1)), 0)
    color, density = rf(nxyz)
    out_colors, log_transmittance = radiance.integrate_path(color, density, ts_padded)
    assert_close(out_colors[-1], torch.tensor([[0.2, 0.2, 0.2]]))

    # Test soft transit and move plane
    rf = ColorGradientRadianceField(
        surface_pos=0.5 * 2 - 1,
        density_scale=1000.0,  # soft surface boundary
        cmap="gray",
    )
    color, density = rf(nxyz)
    out_colors, log_transmittance = radiance.integrate_path(
        color, density, ts_padded, 0
    )

    assert ((out_colors[-1] > 0.5) & (out_colors[-1] < 0.6)).all()


def test_radiance_integrate_path_in_parts():

    ts = torch.rand(30, 20, 1)
    ts_padded = torch.cat((ts, torch.tensor(1.0).view(1, 1, 1).expand(1, 20, 1)), 0)
    color = torch.randn(30, 20, 3)
    density = torch.rand(30, 20, 1) * 1e-1

    full_colors, full_log_transmittance = radiance.integrate_path(
        color, density, ts_padded
    )

    # Compare with part based intrg
    color_parts = []
    log_transm_parts = []

    prev_log_transm = 0.0
    prev_color = 0.0
    for i in [0, 10, 20]:
        colors, log_transm = radiance.integrate_path(
            color[i : i + 10],
            density[i : i + 10],
            ts_padded[i : i + 11],
            prev_log_transmittance=prev_log_transm,
        )
        colors = colors + prev_color
        log_transm = log_transm + prev_log_transm
        color_parts.append(colors)
        if i == 20:
            log_transm_parts.append(log_transm)
        else:
            log_transm_parts.append(log_transm[:-1])
        prev_color = colors[-1:].clone()
        prev_log_transm = log_transm[-1:].clone()

    part_colors = torch.cat(color_parts, 0)

    part_log_transmittance = torch.cat(log_transm_parts, 0)

    assert_close(full_log_transmittance, part_log_transmittance)
    assert_close(full_colors, part_colors)


def test_radiance_rasterize_field():

    rf = ColorGradientRadianceField(
        surface_pos=0.5 * 2 - 1,
        surface_dim=0,
        density_scale=float("inf"),  # hard surface boundary
        cmap="gray",
    )

    # Note, rasterization is not the same as integration
    # We only query the volume at particular locations
    color, sigma = radiance.rasterize_field(rf, resolution=(2, 2, 2))
    assert color.shape == (2, 2, 2, 3)
    assert sigma.shape == (2, 2, 2, 1)

    # Note sigma is D,H,W but indexed x,y,z
    assert (sigma[:, :, 0] < 1e-5).all()
    assert not (torch.isfinite(sigma[:, :, 1])).any()

    # matplotlib colormaps are not exact
    assert ((color[:, :, 0] - 0.25) < 1e-2).all()
    assert ((color[:, :, 1] - 0.75) < 1e-2).all()


@torch.no_grad()
def test_radiance_nerf_module():
    nerf = radiance.NeRF(
        n_colors=3,
        n_hidden=16,
        n_encodings=2**8,
        n_levels=4,
        min_res=16,
        max_res=64,
        is_hdr=False,
    )

    rgb, d = nerf(torch.randn(10, 5, 20, 3))
    assert d.shape == (10, 5, 20, 1)
    assert rgb.shape == (10, 5, 20, 3)
