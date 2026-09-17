from __future__ import annotations

import unittest

from desktop_automation_mcp import platform_win32, target, visibility


class _RectApi:
    def __init__(self, rect: tuple[int, int, int, int] = (10, 20, 210, 120)) -> None:
        self.rect = rect
        self.calls: list[int] = []

    def GetWindowRect(self, hwnd: int, rect_pointer) -> bool:
        self.calls.append(hwnd)
        rect = rect_pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = self.rect
        return True


class _VirtualScreenApi:
    def __init__(self, values: dict[int, int]) -> None:
        self.values = values
        self.calls: list[int] = []

    def GetSystemMetrics(self, metric: int) -> int:
        self.calls.append(metric)
        return self.values[metric]


class Win32AdapterTests(unittest.TestCase):
    def test_target_resolves_adapter_at_call_time_and_restores_it(self) -> None:
        original = platform_win32.get_win32_adapter()
        fake_user32 = _RectApi()
        fake = platform_win32.Win32Adapter(
            user32=fake_user32,
            kernel32=object(),
        )

        with platform_win32.use_win32_adapter(fake):
            rect = target._window_rect(42)
            self.assertIs(platform_win32.get_win32_adapter(), fake)

        self.assertEqual(
            (rect.left, rect.top, rect.right, rect.bottom), (10, 20, 210, 120)
        )
        self.assertEqual(fake_user32.calls, [42])
        self.assertIs(platform_win32.get_win32_adapter(), original)

    def test_scoped_adapter_is_restored_after_exception(self) -> None:
        original = platform_win32.get_win32_adapter()
        fake = platform_win32.Win32Adapter(user32=object(), kernel32=object())

        with self.assertRaisesRegex(RuntimeError, "simulated"):
            with platform_win32.use_win32_adapter(fake):
                raise RuntimeError("simulated")

        self.assertIs(platform_win32.get_win32_adapter(), original)

    def test_negative_screen_origins_preserve_window_relative_points(self) -> None:
        # These are post-DPI, physical-pixel rectangles. The scale labels
        # document the deterministic matrix case; they do not pretend to
        # emulate Windows DPI virtualization, which needs a real-host smoke.
        cases = (
            (100, (-1920, 0, -920, 800), (50, 60), (-1870, 60)),
            (125, (-2560, -200, -1310, 800), (625, 500), (-1935, 300)),
            (150, (-3840, -300, -2340, 900), (1499, 1199), (-2341, 899)),
        )
        for scale_percent, rect, relative, expected in cases:
            with self.subTest(scale_percent=scale_percent):
                fake = platform_win32.Win32Adapter(
                    user32=_RectApi(rect),
                    kernel32=object(),
                )
                with platform_win32.use_win32_adapter(fake):
                    self.assertEqual(
                        visibility._absolute_point(42, *relative), expected
                    )

    def test_virtual_screen_bounds_preserve_negative_origin(self) -> None:
        api = _VirtualScreenApi(
            {
                platform_win32.SM_XVIRTUALSCREEN: -1920,
                platform_win32.SM_YVIRTUALSCREEN: -200,
                platform_win32.SM_CXVIRTUALSCREEN: 4480,
                platform_win32.SM_CYVIRTUALSCREEN: 1800,
            }
        )
        adapter = platform_win32.Win32Adapter(user32=api, kernel32=object())
        with platform_win32.use_win32_adapter(adapter):
            self.assertEqual(
                platform_win32.virtual_screen_bounds(), (-1920, -200, 2560, 1600)
            )

    def test_virtual_screen_bounds_reject_non_positive_dimensions(self) -> None:
        api = _VirtualScreenApi(
            {
                platform_win32.SM_XVIRTUALSCREEN: 0,
                platform_win32.SM_YVIRTUALSCREEN: 0,
                platform_win32.SM_CXVIRTUALSCREEN: 0,
                platform_win32.SM_CYVIRTUALSCREEN: 1080,
            }
        )
        adapter = platform_win32.Win32Adapter(user32=api, kernel32=object())
        with platform_win32.use_win32_adapter(adapter):
            with self.assertRaisesRegex(platform_win32.PlatformError, "Invalid"):
                platform_win32.virtual_screen_bounds()


if __name__ == "__main__":
    unittest.main()
