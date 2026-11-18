"""Gray storm cores in the infrared are painted like the legend's "Tormenta fuerte"."""
import numpy as np
from PIL import Image

from custom_components.smn_ar.satellite import STORM_LEGEND, paint_storm_cores

GRAY = (150, 150, 150, 255)  # warm clouds use grays too
RED = (128, 0, 0, 255)  # -66 °C, the storm's rim
VIOLET = STORM_LEGEND[-1][0]


def _storm(core_radius: int) -> Image.Image:
    size = 80
    yy, xx = np.mgrid[:size, :size]
    distance = np.hypot(yy - 40, xx - 40)
    pixels = np.empty((size, size, 4), dtype=np.uint8)
    pixels[:] = GRAY
    pixels[(distance > core_radius) & (distance <= core_radius + 4)] = RED
    return Image.fromarray(pixels, "RGBA")


def test_a_wide_gray_core_is_painted_whole() -> None:
    painted = np.array(paint_storm_cores(_storm(core_radius=9)))
    assert tuple(painted[40, 40, :3]) == VIOLET  # the very center, 9 px from the rim
    assert tuple(painted[40, 32, :3]) == VIOLET
    assert tuple(painted[40, 46, :3]) == VIOLET
    assert tuple(painted[40, 51, :3]) == (128, 0, 0)  # the rim (10-13 px out) stays red
    assert tuple(painted[2, 2, :3]) == (150, 150, 150)  # warm clouds outside stay gray


def test_no_storm_no_change() -> None:
    clouds = Image.new("RGBA", (40, 40), GRAY)
    assert paint_storm_cores(clouds) is clouds
