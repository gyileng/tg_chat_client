import ctypes
import logging
import sys
from ctypes import wintypes

logger = logging.getLogger(__name__)


def cancel_windows_composition(hwnd):
    """Cancel native candidates without changing the user's input method or language."""
    if sys.platform != "win32":
        return
    imm32 = ctypes.WinDLL("imm32", use_last_error=True)
    imm32.ImmGetContext.argtypes = [wintypes.HWND]
    imm32.ImmGetContext.restype = wintypes.HANDLE
    imm32.ImmNotifyIME.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    imm32.ImmNotifyIME.restype = wintypes.BOOL
    imm32.ImmReleaseContext.argtypes = [wintypes.HWND, wintypes.HANDLE]
    imm32.ImmReleaseContext.restype = wintypes.BOOL
    context = imm32.ImmGetContext(hwnd)
    if not context:
        logger.info("Native IME cancellation: no context")
        return
    try:
        # NI_COMPOSITIONSTR / CPS_CANCEL clears the composition; it does not commit it.
        cancelled = imm32.ImmNotifyIME(context, 0x0015, 0x0004, 0)
        # Candidate lists are a separate state (not all IMEs close them on CPS_CANCEL).
        closed = imm32.ImmNotifyIME(context, 0x0011, 0, 0)  # NI_CLOSECANDIDATE, list 0
        logger.info(
            "Native IME cancellation: composition=%s candidates=%s", bool(cancelled), bool(closed)
        )
    finally:
        imm32.ImmReleaseContext(hwnd, context)
