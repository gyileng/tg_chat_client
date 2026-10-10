from unittest.mock import Mock

import pytest

from tg_chat_client import native_ime


@pytest.mark.parametrize("context", [None, 0x123456789ABC])
def test_windows_candidate_cancel_releases_64_bit_context(monkeypatch, context):
    imm32 = Mock()
    imm32.ImmGetContext.return_value = context
    monkeypatch.setattr(native_ime.sys, "platform", "win32")
    monkeypatch.setattr(native_ime.ctypes, "WinDLL", lambda *a, **kw: imm32, raising=False)
    hwnd = 0xABCDE1234567
    native_ime.cancel_windows_composition(hwnd)
    imm32.ImmGetContext.assert_called_once_with(hwnd)
    if context:
        imm32.ImmNotifyIME.assert_called_once_with(context, 0x0015, 0x0004, 0)
        imm32.ImmReleaseContext.assert_called_once_with(hwnd, context)
        assert imm32.ImmGetContext.restype is native_ime.wintypes.HANDLE
    else:
        imm32.ImmNotifyIME.assert_not_called()
        imm32.ImmReleaseContext.assert_not_called()


def test_windows_candidate_cancel_releases_context_on_error(monkeypatch):
    imm32 = Mock()
    imm32.ImmGetContext.return_value = 123
    imm32.ImmNotifyIME.side_effect = OSError("simulated native failure")
    monkeypatch.setattr(native_ime.sys, "platform", "win32")
    monkeypatch.setattr(native_ime.ctypes, "WinDLL", lambda *a, **kw: imm32, raising=False)
    with pytest.raises(OSError):
        native_ime.cancel_windows_composition(456)
    imm32.ImmReleaseContext.assert_called_once_with(456, 123)
