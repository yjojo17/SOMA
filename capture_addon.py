"""Build and install the IG capture WebExtension into a geckodriver Firefox."""
import zipfile
from pathlib import Path

from selenium.webdriver.firefox.webdriver import WebDriver as _FirefoxWebDriver

_DEFAULT_EXT_DIR = str(Path(__file__).resolve().parent / "ig_capture_extension")


def build_xpi(src_dir: str = _DEFAULT_EXT_DIR,
              out_path: str = "ig_capture_extension.xpi") -> str:
    """Zip the extension directory into an .xpi (manifest.json must land at the root)."""
    src = Path(src_dir)
    out = Path(out_path)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in src.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(src))
    return str(out.resolve())


def install_capture_extension(driver, src_dir: str = _DEFAULT_EXT_DIR) -> None:
    """Temporarily install the capture addon (no signing needed).

    undetected_geckodriver's wrapper subclasses Selenium's generic RemoteWebDriver,
    so it doesn't carry the Firefox convenience method `install_addon`. But its
    command executor is a FirefoxRemoteConnection that does know the INSTALL_ADDON
    command, so we borrow Selenium's own implementation, bound to the wrapper
    instance. Selenium builds the version-correct payload for us.
    """
    xpi = build_xpi(src_dir)
    _FirefoxWebDriver.install_addon(driver, xpi, temporary=True)