import atexit
import base64
import binascii
import re
import subprocess
import threading
from collections import deque
from contextlib import contextmanager

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from app import logger
from app.xray.config import XRayConfig
from config import DEBUG


def _b64_decode(value: str) -> bytes:
    """Decode a key the way Xray writes them: base64url, padding stripped."""
    value = value.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(value + "=" * (-len(value) % 4))


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class XRayCore:
    def __init__(self,
                 executable_path: str = "/usr/bin/xray",
                 assets_path: str = "/usr/share/xray"):
        self.executable_path = executable_path
        self.assets_path = assets_path

        self.version = self.get_version()
        self.process = None
        self.restarting = False

        self._logs_buffer = deque(maxlen=100)
        self._temp_log_buffers = {}
        self._on_start_funcs = []
        self._on_stop_funcs = []
        self._env = {
            "XRAY_LOCATION_ASSET": assets_path
        }

        atexit.register(lambda: self.stop() if self.started else None)

    def get_version(self):
        cmd = [self.executable_path, "version"]
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode('utf-8')
        m = re.match(r'^Xray (\d+\.\d+\.\d+)', output)
        if m:
            return m.groups()[0]

    def get_x25519(self, private_key: str = None):
        cmd = [self.executable_path, "x25519"]
        if private_key:
            cmd.extend(['-i', private_key])
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode('utf-8')
        data = {}
        for line in output.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                data[key.strip()] = value.strip()
        
        keys_lower = {k.lower().replace(' ', '').replace('(', '').replace(')', ''): v for k, v in data.items()}
        prv = keys_lower.get("privatekey")
        pub = keys_lower.get("publickey") or keys_lower.get("password") or keys_lower.get("passwordpublickey")
        
        if prv and pub:
            return {
                "private_key": prv,
                "public_key": pub
            }

    def __capture_process_logs(self):
        def capture_and_debug_log():
            while self.process:
                output = self.process.stdout.readline()
                if output:
                    output = output.strip()
                    self._logs_buffer.append(output)
                    for buf in list(self._temp_log_buffers.values()):
                        buf.append(output)
                    logger.debug(output)

                elif not self.process or self.process.poll() is not None:
                    break

        def capture_only():
            while self.process:
                output = self.process.stdout.readline()
                if output:
                    output = output.strip()
                    self._logs_buffer.append(output)
                    for buf in list(self._temp_log_buffers.values()):
                        buf.append(output)

                elif not self.process or self.process.poll() is not None:
                    break

        if DEBUG:
            threading.Thread(target=capture_and_debug_log).start()
        else:
            threading.Thread(target=capture_only).start()

    @contextmanager
    def get_logs(self):
        buf = deque(self._logs_buffer, maxlen=100)
        buf_id = id(buf)
        try:
            self._temp_log_buffers[buf_id] = buf
            yield buf
        finally:
            del self._temp_log_buffers[buf_id]
            del buf

    @property
    def started(self):
        if not self.process:
            return False

        if self.process.poll() is None:
            return True

        return False

    def start(self, config: XRayConfig):
        if self.started is True:
            raise RuntimeError("Xray is started already")

        if config.get('log', {}).get('logLevel') in ('none', 'error'):
            config['log']['logLevel'] = 'warning'

        cmd = [
            self.executable_path,
            "run",
            '-config',
            'stdin:'
        ]
        self.process = subprocess.Popen(
            cmd,
            env=self._env,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
            universal_newlines=True
        )
        self.process.stdin.write(config.to_json())
        self.process.stdin.flush()
        self.process.stdin.close()
        logger.warning(f"Xray core {self.version} started")

        self.__capture_process_logs()

        # execute on start functions
        for func in self._on_start_funcs:
            threading.Thread(target=func).start()

    def stop(self):
        if not self.started:
            return

        self.process.terminate()
        self.process = None
        logger.warning("Xray core stopped")

        # execute on stop functions
        for func in self._on_stop_funcs:
            threading.Thread(target=func).start()

    def restart(self, config: XRayConfig):
        if self.restarting is True:
            return

        try:
            self.restarting = True
            logger.warning("Restarting Xray core...")
            self.stop()
            self.start(config)
        finally:
            self.restarting = False

    def on_start(self, func: callable):
        self._on_start_funcs.append(func)
        return func

    def on_stop(self, func: callable):
        self._on_stop_funcs.append(func)
        return func


class NullXRayCore:
    """Stand-in used when the panel runs as a pure control plane.

    Implemented as a null object rather than ``if`` statements at every call
    site, for one concrete reason: ``XRayCore.__init__`` shells out to
    ``xray version``, so on a panel image without the binary the *import* of
    ``app.xray`` would fail. This keeps every existing caller working while
    the panel owns no local dataplane.

    Note that ``started`` stays False rather than lying, so ``GET /api/core``
    reports the truth.
    """

    def __init__(self, *args, **kwargs):
        self.executable_path = None
        self.assets_path = None
        self.version = None
        self.process = None
        self.restarting = False
        self._on_start_funcs = []
        self._on_stop_funcs = []

    def get_version(self):
        return None

    def get_x25519(self, private_key: str = None):
        """Derive (or generate) a reality keypair without the xray binary.

        This is not a convenience. ``XRayConfig`` calls it for every reality
        inbound that ships a ``privateKey`` but no ``publicKey`` -- the shape
        Xray itself produces -- and raises if it comes back empty. Returning
        None here would make a coreless panel fail to even import its own
        config. X25519 is plain scalar multiplication and ``cryptography`` is
        already a dependency, so the result is byte-identical to
        ``xray x25519 -i``.
        """
        try:
            if private_key:
                raw = _b64_decode(private_key)
                if len(raw) != 32:
                    return None
                key = X25519PrivateKey.from_private_bytes(raw)
            else:
                key = X25519PrivateKey.generate()

            private_raw = key.private_bytes(
                serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,
                serialization.NoEncryption(),
            )
            public_raw = key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        except (ValueError, TypeError, binascii.Error):
            return None

        return {
            "private_key": _b64_encode(private_raw),
            "public_key": _b64_encode(public_raw),
        }

    @contextmanager
    def get_logs(self):
        yield deque(maxlen=100)

    @property
    def started(self):
        return False

    def start(self, config: XRayConfig):
        return None

    def stop(self):
        return None

    def restart(self, config: XRayConfig):
        return None

    def on_start(self, func: callable):
        self._on_start_funcs.append(func)
        return func

    def on_stop(self, func: callable):
        self._on_stop_funcs.append(func)
        return func
