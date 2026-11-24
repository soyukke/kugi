#!/usr/bin/env python3
"""End-to-end integration tests for kugi using a local loopback mock HTTP server."""

import hashlib
import http.server
import io
import json
import os
import platform
import shutil
import socketserver
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time


def make_tar_gz(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for item in entries:
            kind = item.get("kind", "file")
            ti = tarfile.TarInfo(item["name"])
            if kind == "file":
                data = item["data"]
                ti.size = len(data)
                ti.mode = item.get("mode", 0o755)
                tf.addfile(ti, io.BytesIO(data))
            elif kind == "symlink":
                ti.type = tarfile.SYMTYPE
                ti.linkname = item["target"]
                tf.addfile(ti)
    return buf.getvalue()


class MockRegistry:
    def __init__(self):
        self.lock = threading.Lock()
        self.routes = {}
        self.redirects = {}
        self.errors = {}
        self.delays = {}
        self.counts = {}

    def put(self, path, payload, delay=0.0):
        with self.lock:
            self.routes[path] = payload
            self.delays[path] = delay

    def redirect(self, path, location):
        with self.lock:
            self.redirects[path] = location

    def error(self, path, status, body=b"error"):
        with self.lock:
            self.errors[path] = (status, body)

    def count(self, path):
        with self.lock:
            return self.counts.get(path, 0)

    def reset_counts(self):
        with self.lock:
            self.counts.clear()


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ThreadingHTTPSServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, ssl_context):
        self.ssl_context = ssl_context
        super().__init__(server_address, handler_class)

    def process_request_thread(self, request, client_address):
        try:
            tls_sock = self.ssl_context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            try:
                request.close()
            except OSError:
                pass
            return
        super().process_request_thread(tls_sock, client_address)

    def handle_error(self, request, client_address):
        return


def make_handler(registry):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            with registry.lock:
                registry.counts[self.path] = registry.counts.get(self.path, 0) + 1
                err_resp = registry.errors.get(self.path)
                redir = registry.redirects.get(self.path)
                payload = registry.routes.get(self.path)
                delay = registry.delays.get(self.path, 0.0)
            if err_resp is not None:
                status, body = err_resp
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if redir is not None:
                self.send_response(302)
                self.send_header("Location", redir)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if payload is None:
                self.send_response(404)
                self.send_header("Content-Length", "9")
                self.end_headers()
                self.wfile.write(b"not found")
                return
            if delay > 0:
                time.sleep(delay)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def start_mock_server(registry):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(registry))
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, port


def generate_self_signed_cert(out_dir, prefix):
    key_path = os.path.join(out_dir, f"{prefix}-key.pem")
    cert_path = os.path.join(out_dir, f"{prefix}-cert.pem")
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-pkeyopt",
            "ec_param_enc:named_curve",
            "-nodes",
            "-keyout",
            key_path,
            "-out",
            cert_path,
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert_path, key_path


def start_mock_https_server(registry, cert_path, key_path, tls_version):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = tls_version
    ctx.maximum_version = tls_version
    ctx.load_cert_chain(cert_path, key_path)
    srv = ThreadingHTTPSServer(("127.0.0.1", 0), make_handler(registry), ctx)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, port


def run_kugi(kugi_bin, args, env, cwd=None):
    return subprocess.run(
        [kugi_bin] + args,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


def assert_eq(expected, actual, msg=""):
    if expected != actual:
        raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")


def assert_true(cond, msg=""):
    if not cond:
        raise AssertionError(msg or "expected condition to be true")


def tool_store_key(mode, sha, bin_specs):
    spec = "\n".join(sorted(bin_specs)).encode("utf-8")
    spec_hex = hashlib.sha256(spec).hexdigest()[:8]
    return f"tool-{mode}-{sha[:16]}-{spec_hex}"


def asset_store_key(sha, unwrap=True, raw_filename=None):
    if raw_filename is not None:
        name_hex = hashlib.sha256(raw_filename.encode("utf-8")).hexdigest()[:8]
        return f"asset-file-{sha[:16]}-{name_hex}"
    if unwrap:
        return f"asset-{sha[:16]}"
    return f"asset-tree-{sha[:16]}"


def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    kugi_bin = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(repo_root, "dist", "kugi")
    assert_true(os.path.isfile(kugi_bin), f"kugi binary not found at {kugi_bin}")

    reg = MockRegistry()
    srv, port = start_mock_server(reg)
    base_url = f"http://127.0.0.1:{port}"

    strip_tar = make_tar_gz([
        {"name": "alpha-1.0.0/alpha", "data": b"#!/bin/sh\necho alpha-v1\n"},
        {"name": "alpha-1.0.0/README.md", "data": b"should be stripped\n", "mode": 0o644},
    ])
    full_tar = make_tar_gz([
        {
            "name": "ztool-2.0.0/ztool",
            "data": b'#!/bin/sh\nREAL=$(python3 -c "import os,sys; print(os.path.dirname(os.path.realpath(sys.argv[1])))" "$0")\ncat "$REAL/lib/greeting.txt"\n',
        },
        {"name": "ztool-2.0.0/lib/greeting.txt", "data": b"hello-from-lib\n", "mode": 0o644},
    ])
    nodelike_tar = make_tar_gz([
        {"name": "nodelike-1.0.0/bin/runtime", "data": b"#!/bin/sh\necho runtime-ok\n"},
        {"name": "nodelike-1.0.0/bin/cli", "kind": "symlink", "target": "../lib/cli.sh"},
        {
            "name": "nodelike-1.0.0/lib/cli.sh",
            "data": b'#!/bin/sh\nBASE=$(dirname "$0")\ncat "$BASE/../lib/data.txt"\n',
            "mode": 0o644,
        },
        {"name": "nodelike-1.0.0/lib/data.txt", "data": b"from-relative-lib\n", "mode": 0o644},
        {"name": "nodelike-1.0.0/lib/decoy/cli", "data": b"#!/bin/sh\necho wrong-decoy\n"},
    ])
    symbin_tar = make_tar_gz([
        {"name": "sympkg-1.0.0/bin/symtool", "kind": "symlink", "target": "../libexec/symtool.sh"},
        {"name": "sympkg-1.0.0/libexec/symtool.sh", "data": b"#!/bin/sh\necho symbin-ok\n", "mode": 0o644},
    ])
    raw_bin = b"#!/bin/sh\necho raw-tool-ok\n"
    boot_bin = b"#!/bin/sh\necho boot-v1\n"
    shared_tar = make_tar_gz([
        {"name": "shared-1.0.0/shared", "data": b"#!/bin/sh\necho shared-ok\n"},
    ])
    ephemeral_tar = make_tar_gz([
        {"name": "eph-1.0.0/eph", "data": b"#!/bin/sh\necho eph-ok\n"},
    ])
    oneshot_tar = make_tar_gz([
        {"name": "oneshot-1.0.0/bin/oneshot", "data": b"#!/bin/sh\necho oneshot-ok\n"},
    ])
    oneshot_boot = b"#!/bin/sh\necho oneshot-boot-ok\n"
    oneshot_global_tar = make_tar_gz([
        {"name": "globtool-1.0.0/globtool", "data": b"#!/bin/sh\necho globtool-ok\n"},
    ])
    traversal_tar = make_tar_gz([
        {"name": "../escaped.txt", "data": b"pwned\n"},
    ])
    symlink_escape_tar = make_tar_gz([
        {"name": "pkg/evil", "kind": "symlink", "target": "../../etc/passwd"},
    ])

    reg.put("/downloads/alpha-1.0.0.tar.gz", strip_tar)
    reg.redirect("/redir/alpha-1.0.0.tar.gz", "/downloads/alpha-1.0.0.tar.gz")
    reg.put("/downloads/ztool-2.0.0.tar.gz", full_tar)
    reg.put("/downloads/nodelike-1.0.0.tar.gz", nodelike_tar)
    reg.put("/downloads/symbin-1.0.0.tar.gz", symbin_tar)
    reg.put("/downloads/rawtool-1.0.0", raw_bin)
    reg.put("/downloads/boot-1.0.0", boot_bin)
    reg.put("/downloads/shared-1.0.0.tar.gz", shared_tar, delay=0.15)
    reg.put("/downloads/ephemeral-1.0.0.tar.gz", ephemeral_tar)
    reg.put("/downloads/oneshot-1.0.0.tar.gz", oneshot_tar)
    reg.put("/downloads/oneshot-boot-1.0.0", oneshot_boot)
    reg.put("/downloads/globtool-1.0.0.tar.gz", oneshot_global_tar)
    reg.put("/downloads/traversal.tar.gz", traversal_tar)
    reg.put("/downloads/symlink_escape.tar.gz", symlink_escape_tar)

    fonts_tar = make_tar_gz([
        {"name": "fonts-1.0.0/otf/regular.otf", "data": b"font-otf-bytes\n", "mode": 0o644},
        {"name": "fonts-1.0.0/LICENSE", "data": b"MIT\n", "mode": 0o644},
    ])
    bundle_tar = make_tar_gz([
        {"name": "bundle-1.0.0/meta.txt", "data": b"bundle-meta\n", "mode": 0o644},
    ])
    dict_raw = b"dict-raw-payload\n"
    reg.put("/downloads/fonts-1.0.0.tar.gz", fonts_tar)
    reg.put("/downloads/bundle-1.0.0.tar.gz", bundle_tar)
    reg.put("/downloads/dict-1.0.0.dat", dict_raw)

    multi_payloads = {}
    for target in ("darwin-aarch64", "darwin-x86_64", "linux-aarch64", "linux-x86_64"):
        tool_tar = make_tar_gz([
            {"name": f"mtool-1.0.0/mtool", "data": f"#!/bin/sh\necho mtool-{target}\n".encode("utf-8")},
        ])
        pack_tar = make_tar_gz([
            {"name": f"mpack-1.0.0/info.txt", "data": f"pack-{target}\n".encode("utf-8"), "mode": 0o644},
        ])
        multi_payloads[("tool", target)] = tool_tar
        multi_payloads[("pack", target)] = pack_tar
        reg.put(f"/multi/mtool-1.0.0-{target}.tar.gz", tool_tar)
        reg.put(f"/multi/mpack-1.0.0-{target}.tar.gz", pack_tar)

    host_os = "darwin" if sys.platform == "darwin" else "linux"
    mach = platform.machine().lower()
    host_arch = "aarch64" if mach in ("arm64", "aarch64") else "x86_64"
    host_target = f"{host_os}-{host_arch}"
    other_target = "linux-x86_64" if host_target != "linux-x86_64" else "darwin-aarch64"

    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="kugi-int-") as root:
        home_dir = os.path.join(root, "home")
        kugi_home = os.path.join(root, "kugi_home")
        xdg_config = os.path.join(root, "xdg_config")
        os.makedirs(home_dir)
        os.makedirs(xdg_config)

        base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": home_dir,
            "KUGI_HOME": kugi_home,
            "XDG_CONFIG_HOME": xdg_config,
            "TMPDIR": os.path.join(root, "sys_tmp"),
        }
        os.makedirs(base_env["TMPDIR"])

        # 1. Version & CLI exit codes
        r = run_kugi(kugi_bin, ["--version"], base_env)
        assert_eq(0, r.returncode, "version rc")
        assert_eq("0.1.0", r.stdout.strip(), "version output")

        r = run_kugi(kugi_bin, ["unknown-cmd"], base_env)
        assert_eq(2, r.returncode, "unknown cmd exit code 2")

        r = run_kugi(kugi_bin, ["exec"], base_env)
        assert_eq(2, r.returncode, "exec missing -- exit code 2")

        # 2. Invalid manifest returns exit code 2
        bad_dir = os.path.join(root, "bad_manifest")
        os.makedirs(bad_dir)
        with open(os.path.join(bad_dir, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write('[tools.bad]\nversion = "latest"\nurl = "https://example.com/a"\nbin = ["a"]\n')
        r = run_kugi(kugi_bin, ["lock"], base_env, cwd=bad_dir)
        assert_eq(2, r.returncode, "unpinned latest version must exit 2")

        # 3. Full lock -> sync -> exec -> env -> status happy path
        proj = os.path.join(root, "proj")
        os.makedirs(proj)
        dot_src = os.path.join(home_dir, "my-configs", "rc")
        os.makedirs(os.path.dirname(dot_src))
        with open(dot_src, "w", encoding="utf-8") as f:
            f.write("config-content\n")

        manifest_text = f'''[tools.alpha]
version = "1.0.0"
url = "{base_url}/redir/alpha-{{version}}.tar.gz"
bin = ["alpha"]

[tools.ztool]
version = "2.0.0"
url = "{base_url}/downloads/ztool-{{version}}.tar.gz"
extract = "all"
bin = ["ztool"]

[tools.rawtool]
version = "1.0.0"
url = "{base_url}/downloads/rawtool-{{version}}"
archive = "none"
bin = ["rawtool-orig:rawtool"]

[tools.boot]
version = "1.0.0"
url = "{base_url}/downloads/boot-{{version}}"
archive = "none"
mode = "bootstrap"
install_dir = "~/.boot/bin"
bin = ["boot"]

[env]
CUSTOM_FLAG = "enabled"
DATA_DIR = "~/my-data"
PROJECT_ROOT = "{{manifest_dir}}"
LIB_PATHS = "{{manifest_dir}}/lib:{{manifest_dir}}/ext"

[links]
"~/.config/demo/rc" = "~/my-configs/rc"
'''
        manifest_path = os.path.join(proj, "kugi.toml")
        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write(manifest_text)

        # sync before lock must fail clearly
        r = run_kugi(kugi_bin, ["sync"], base_env, cwd=proj)
        assert_eq(1, r.returncode, "sync without lock must exit 1")
        assert_true("kugi lock" in r.stderr, f"expected kugi lock hint, got: {r.stderr}")

        # status before lock reports unlocked
        r = run_kugi(kugi_bin, ["status"], base_env, cwd=proj)
        assert_eq(0, r.returncode, f"status unlocked failed: {r.stderr}")
        assert_true("alpha 1.0.0 unlocked" in r.stdout, r.stdout)

        # lock (plain `kugi lock` must not mutate store, roots, bootstrap, or links)
        r = run_kugi(kugi_bin, ["lock"], base_env, cwd=proj)
        assert_eq(0, r.returncode, f"lock failed: {r.stderr}")
        lock_path = os.path.join(proj, "kugi.lock")
        assert_true(os.path.isfile(lock_path), "kugi.lock not created")
        assert_eq([], os.listdir(os.path.join(kugi_home, "store")), "plain lock must not populate store")
        assert_eq([], os.listdir(os.path.join(kugi_home, "roots")), "plain lock must not register roots")
        assert_true(not os.path.exists(os.path.join(home_dir, ".boot", "bin", "boot")), "plain lock must not install bootstrap")
        assert_true(not os.path.exists(os.path.join(home_dir, ".config", "demo", "rc")), "plain lock must not apply links")
        with open(lock_path, "r", encoding="utf-8") as f:
            lock_raw = f.read()
        assert_true(lock_raw.endswith("\n"), "kugi.lock missing trailing newline")
        lock_json = json.loads(lock_raw)
        assert_eq(1, lock_json["version"], "lock version")
        assert_eq(["alpha", "boot", "rawtool", "ztool"], list(lock_json["tools"].keys()), "sorted lock tool keys")

        # sync
        r = run_kugi(kugi_bin, ["sync"], base_env, cwd=proj)
        assert_eq(0, r.returncode, f"sync failed: {r.stderr}")
        assert_eq([], os.listdir(os.path.join(kugi_home, "tmp")), "tmp dir must be clean after sync")

        # Verify strip extraction removed README.md for alpha, kept lib/greeting.txt for ztool
        store_entries = os.listdir(os.path.join(kugi_home, "store"))
        alpha_sha_main = hashlib.sha256(strip_tar).hexdigest()
        ztool_sha_main = hashlib.sha256(full_tar).hexdigest()
        alpha_dir = tool_store_key("bin", alpha_sha_main, ["alpha:alpha"])
        assert_true(alpha_dir in store_entries, f"expected {alpha_dir} in {store_entries}")
        assert_true(os.path.isfile(os.path.join(kugi_home, "store", alpha_dir, "bin", "alpha")))
        assert_true(not os.path.exists(os.path.join(kugi_home, "store", alpha_dir, "alpha-1.0.0")))

        ztool_dir = tool_store_key("all", ztool_sha_main, ["ztool:ztool"])
        assert_true(ztool_dir in store_entries, f"expected {ztool_dir} in {store_entries}")
        assert_true(os.path.isfile(os.path.join(kugi_home, "store", ztool_dir, "lib", "greeting.txt")))
        assert_true(not os.path.exists(os.path.join(kugi_home, "store", ztool_dir, "ztool-2.0.0")))

        # Verify [links] created symlink
        link_dest = os.path.join(home_dir, ".config", "demo", "rc")
        assert_true(os.path.islink(link_dest), "link dest should be symlink")
        assert_eq(dot_src, os.readlink(link_dest), "link target mismatch")

        # Verify kugi exec runs binaries + sees [env] (including ~ and {manifest_dir})
        proj_real = os.path.realpath(proj)
        r = run_kugi(
            kugi_bin,
            [
                "exec",
                "--",
                "sh",
                "-c",
                'printf "%s|%s|%s|%s|%s|%s|%s" "$(alpha)" "$(rawtool)" "$(boot)" "$CUSTOM_FLAG" "$DATA_DIR" "$PROJECT_ROOT" "$LIB_PATHS"',
            ],
            base_env,
            cwd=proj,
        )
        assert_eq(0, r.returncode, f"exec failed: {r.stderr}")
        assert_eq(
            f"alpha-v1|raw-tool-ok|boot-v1|enabled|{home_dir}/my-data|{proj_real}|{proj_real}/lib:{proj_real}/ext",
            r.stdout,
            "exec output mismatch",
        )

        # Verify extract = "all" tool executes via kugi exec and resolves sibling lib/greeting.txt
        ztool_bin = os.path.join(kugi_home, "store", ztool_dir, "bin", "ztool")
        assert_true(os.path.islink(ztool_bin), "extract=all bin should be symlink")
        r = run_kugi(kugi_bin, ["exec", "--", "ztool"], base_env, cwd=proj)
        assert_eq(0, r.returncode, f"exec ztool failed: {r.stderr}")
        assert_eq("hello-from-lib", r.stdout.strip())

        # Verify Node.js-like layout (unwrapped single top dir + bin/cli -> ../lib/cli.sh) and extract=bin symlink target copy
        nodelike_proj = os.path.join(root, "nodelike_proj")
        os.makedirs(nodelike_proj)
        nodelike_sha = hashlib.sha256(nodelike_tar).hexdigest()
        symbin_sha = hashlib.sha256(symbin_tar).hexdigest()
        with open(os.path.join(nodelike_proj, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                f'[tools.nodelike]\nversion = "1.0.0"\nurl = "{base_url}/downloads/nodelike-1.0.0.tar.gz"\n'
                f'extract = "all"\nbin = ["runtime", "cli"]\nsha256 = "{nodelike_sha}"\n\n'
                f'[tools.symbin]\nversion = "1.0.0"\nurl = "{base_url}/downloads/symbin-1.0.0.tar.gz"\n'
                f'extract = "bin"\nbin = ["symtool"]\nsha256 = "{symbin_sha}"\n'
            )
        r = run_kugi(kugi_bin, ["sync"], base_env, cwd=nodelike_proj)
        assert_eq(0, r.returncode, f"nodelike sync failed: {r.stderr}")
        nl_key = tool_store_key("all", nodelike_sha, ["runtime:runtime", "cli:cli"])
        nl_store = os.path.join(kugi_home, "store", nl_key)
        assert_true(not os.path.exists(os.path.join(nl_store, "nodelike-1.0.0")), "single top-level dir must be unwrapped")
        assert_true(os.path.isfile(os.path.join(nl_store, "lib", "data.txt")), "unwrapped lib/data.txt missing")
        cli_bin = os.path.join(nl_store, "bin", "cli")
        assert_true(os.path.islink(cli_bin), "bin/cli symlink must be preserved")
        assert_eq("../lib/cli.sh", os.readlink(cli_bin), "bin/cli symlink target must be preserved")
        r = run_kugi(kugi_bin, ["exec", "--", "cli"], base_env, cwd=nodelike_proj)
        assert_eq(0, r.returncode, f"exec cli failed: {r.stderr}")
        assert_eq("from-relative-lib", r.stdout.strip(), "cli should resolve ../lib/data.txt")
        r = run_kugi(kugi_bin, ["exec", "--", "runtime"], base_env, cwd=nodelike_proj)
        assert_eq(0, r.returncode, f"exec runtime failed: {r.stderr}")
        assert_eq("runtime-ok", r.stdout.strip())

        sb_key = tool_store_key("bin", symbin_sha, ["symtool:symtool"])
        sb_bin = os.path.join(kugi_home, "store", sb_key, "bin", "symtool")
        assert_true(os.path.isfile(sb_bin) and not os.path.islink(sb_bin), "extract=bin must copy symlink target as regular file")
        assert_true(not os.path.exists(os.path.join(kugi_home, "store", sb_key, "libexec")), "extract=bin must strip sibling dirs")
        r = run_kugi(kugi_bin, ["exec", "--", "symtool"], base_env, cwd=nodelike_proj)
        assert_eq(0, r.returncode, f"exec symtool failed: {r.stderr}")
        assert_eq("symbin-ok", r.stdout.strip())

        # Verify upward manifest discovery from a nested subdirectory also resolves {manifest_dir} to manifest root
        nested_sub = os.path.join(proj, "nested", "deep")
        os.makedirs(nested_sub)
        r = run_kugi(kugi_bin, ["exec", "--", "sh", "-c", 'printf "%s|%s" "$(alpha)" "$PROJECT_ROOT"'], base_env, cwd=nested_sub)
        assert_eq(0, r.returncode, f"nested exec failed: {r.stderr}")
        assert_eq(f"alpha-v1|{proj_real}", r.stdout)

        # Verify selective `kugi lock --tool` preserves other lock entries
        r = run_kugi(kugi_bin, ["lock", "--tool", "alpha"], base_env, cwd=proj)
        assert_eq(0, r.returncode, f"selective lock failed: {r.stderr}")
        with open(lock_path, "r", encoding="utf-8") as f:
            selective_lock = json.loads(f.read())
        assert_eq(["alpha", "boot", "rawtool", "ztool"], list(selective_lock["tools"].keys()))

        # Verify URL drift in kugi.toml without re-locking is detected by sync
        drift_dir = os.path.join(root, "drift_proj")
        os.makedirs(drift_dir)
        shutil.copy2(lock_path, os.path.join(drift_dir, "kugi.lock"))
        with open(os.path.join(drift_dir, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(manifest_text.replace("/downloads/rawtool-{version}", "/downloads/rawtool-changed-{version}"))
        r = run_kugi(kugi_bin, ["sync"], base_env, cwd=drift_dir)
        assert_eq(1, r.returncode, "URL drift without re-locking must fail sync")
        assert_true("kugi lock" in r.stderr, r.stderr)

        # Verify kugi env for sh and fish
        r = run_kugi(kugi_bin, ["env", "--shell", "sh"], base_env, cwd=proj)
        assert_eq(0, r.returncode, f"env sh failed: {r.stderr}")
        assert_true("export CUSTOM_FLAG='enabled'" in r.stdout, r.stdout)
        assert_true(f"export DATA_DIR='{home_dir}/my-data'" in r.stdout, r.stdout)
        assert_true(f"export PROJECT_ROOT='{proj_real}'" in r.stdout, r.stdout)
        assert_true(f"export LIB_PATHS='{proj_real}/lib:{proj_real}/ext'" in r.stdout, r.stdout)
        eval_res = subprocess.run(
            ["sh", "-c", r.stdout + '\nprintf "%s|%s|%s" "$(alpha)" "$CUSTOM_FLAG" "$PROJECT_ROOT"'],
            env=base_env,
            capture_output=True,
            text=True,
        )
        assert_eq(0, eval_res.returncode, eval_res.stderr)
        assert_eq(f"alpha-v1|enabled|{proj_real}", eval_res.stdout)

        r = run_kugi(kugi_bin, ["env", "--shell", "fish"], base_env, cwd=proj)
        assert_eq(0, r.returncode, f"env fish failed: {r.stderr}")
        assert_true("set -gx PATH " in r.stdout and "set -gx CUSTOM_FLAG 'enabled'" in r.stdout, r.stdout)
        assert_true(f"set -gx PROJECT_ROOT '{proj_real}'" in r.stdout, r.stdout)
        assert_true(f"set -gx LIB_PATHS '{proj_real}/lib:{proj_real}/ext'" in r.stdout, r.stdout)

        # Verify kugi status after sync
        r = run_kugi(kugi_bin, ["status"], base_env, cwd=proj)
        assert_eq(0, r.returncode, r.stderr)
        assert_true("alpha 1.0.0 ok " in r.stdout, r.stdout)
        assert_true("boot 1.0.0 bootstrap " in r.stdout, r.stdout)
        assert_true("store_total:" in r.stdout and "reclaimable:" in r.stdout, r.stdout)

        # 4. Bootstrap non-overwrite invariant
        boot_bin_path = os.path.join(home_dir, ".boot", "bin", "boot")
        assert_true(os.path.isfile(boot_bin_path))
        with open(boot_bin_path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho self-updated-v99\n")
        os.chmod(boot_bin_path, 0o755)
        reg.reset_counts()
        r = run_kugi(kugi_bin, ["sync", "--force"], base_env, cwd=proj)
        assert_eq(0, r.returncode, f"resync with bootstrap failed: {r.stderr}")
        assert_eq(0, reg.count("/downloads/boot-1.0.0"), "bootstrap tool must not be re-downloaded when already present")
        with open(boot_bin_path, "r", encoding="utf-8") as f:
            assert_true("self-updated-v99" in f.read(), "bootstrap binary was clobbered!")

        # 5. [links] non-symlink protection and --force override
        os.unlink(link_dest)
        with open(link_dest, "w", encoding="utf-8") as f:
            f.write("precious-user-file\n")
        r = run_kugi(kugi_bin, ["sync"], base_env, cwd=proj)
        assert_eq(1, r.returncode, "sync must refuse overwriting regular file in [links]")
        with open(link_dest, "r", encoding="utf-8") as f:
            assert_eq("precious-user-file\n", f.read(), "user file was damaged!")
        r = run_kugi(kugi_bin, ["sync", "--force"], base_env, cwd=proj)
        assert_eq(0, r.returncode, f"sync --force should replace file with symlink: {r.stderr}")
        assert_true(os.path.islink(link_dest), "forced link should be symlink")

        # Symlink pointing elsewhere is atomically updated without --force
        os.unlink(link_dest)
        os.symlink("/nonexistent/other/target", link_dest)
        r = run_kugi(kugi_bin, ["sync"], base_env, cwd=proj)
        assert_eq(0, r.returncode, f"updating wrong symlink should succeed without --force: {r.stderr}")
        assert_eq(dot_src, os.readlink(link_dest), "wrong symlink should be atomically updated")

        # 6. SHA-256 mismatch rejection
        tamper_dir = os.path.join(root, "tamper")
        os.makedirs(tamper_dir)
        with open(os.path.join(tamper_dir, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                f'[tools.tampered]\nversion = "1.0.0"\nurl = "{base_url}/downloads/rawtool-1.0.0"\n'
                f'archive = "none"\nbin = ["tampered"]\n'
                f'sha256 = "0000000000000000000000000000000000000000000000000000000000000000"\n'
            )
        r = run_kugi(kugi_bin, ["sync"], base_env, cwd=tamper_dir)
        assert_eq(1, r.returncode, "sha256 mismatch must fail")
        assert_true("sha256 mismatch" in r.stderr, r.stderr)
        assert_eq([], [e for e in os.listdir(os.path.join(kugi_home, "store")) if e.startswith("tool-bin-0000000000000000-")])
        assert_eq([], os.listdir(os.path.join(kugi_home, "tmp")), "part file must be deleted on hash mismatch")

        # 7. Path traversal & symlink escape rejection
        for bad_name, bad_path, bad_bytes in [
            ("trav", "/downloads/traversal.tar.gz", traversal_tar),
            ("sym", "/downloads/symlink_escape.tar.gz", symlink_escape_tar),
        ]:
            d = os.path.join(root, f"evil_{bad_name}")
            os.makedirs(d)
            with open(os.path.join(d, "kugi.toml"), "w", encoding="utf-8") as f:
                f.write(f'[tools.{bad_name}]\nversion = "1.0.0"\nurl = "{base_url}{bad_path}"\nbin = ["escaped.txt"]\n')
            r = run_kugi(kugi_bin, ["lock"], base_env, cwd=d)
            assert_eq(0, r.returncode, f"lock evil {bad_name} failed: {r.stderr}")
            r = run_kugi(kugi_bin, ["sync"], base_env, cwd=d)
            assert_eq(1, r.returncode, f"sync evil {bad_name} must reject traversal/escape")
            assert_true(not os.path.exists(os.path.join(kugi_home, "tmp", "escaped.txt")))
            bad_sha = hashlib.sha256(bad_bytes).hexdigest()
            assert_eq([], [e for e in os.listdir(os.path.join(kugi_home, "store")) if e.startswith(f"tool-bin-{bad_sha[:16]}-")])

        # 8. Multi-worktree shared store + concurrent parallel sync under flock
        reg.reset_counts()
        shared_sha = hashlib.sha256(shared_tar).hexdigest()
        worktrees = []
        for idx in range(4):
            wt = os.path.join(root, f"wt_{idx}")
            os.makedirs(wt)
            with open(os.path.join(wt, "kugi.toml"), "w", encoding="utf-8") as f:
                f.write(
                    f'[tools.shared]\nversion = "1.0.0"\nurl = "{base_url}/downloads/shared-1.0.0.tar.gz"\n'
                    f'bin = ["shared"]\nsha256 = "{shared_sha}"\n'
                )
            worktrees.append(wt)

        procs = [
            subprocess.Popen([kugi_bin, "sync"], env=base_env, cwd=wt, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for wt in worktrees
        ]
        for p in procs:
            out, err = p.communicate(timeout=30)
            assert_eq(0, p.returncode, f"concurrent sync failed: {err}")

        shared_key = tool_store_key("bin", shared_sha, ["shared:shared"])
        shared_entries = [e for e in os.listdir(os.path.join(kugi_home, "store")) if e == shared_key]
        assert_eq(1, len(shared_entries), f"expected single deduplicated store entry, got {shared_entries}")
        assert_eq(1, reg.count("/downloads/shared-1.0.0.tar.gz"), "concurrent flock sync should download artifact only once")

        # 9. Global manifest fallback & KUGI_HOME/bin/ symlinks + XDG_DATA_HOME fallback
        global_dir = os.path.join(xdg_config, "kugi")
        os.makedirs(global_dir)
        global_manifest = os.path.join(global_dir, "kugi.toml")
        with open(global_manifest, "w", encoding="utf-8") as f:
            f.write(
                f'[tools.shared]\nversion = "1.0.0"\nurl = "{base_url}/downloads/shared-1.0.0.tar.gz"\n'
                f'bin = ["shared"]\nsha256 = "{shared_sha}"\n'
            )
        empty_cwd = os.path.join(root, "empty_cwd")
        os.makedirs(empty_cwd)
        r = run_kugi(kugi_bin, ["sync"], base_env, cwd=empty_cwd)
        assert_eq(0, r.returncode, f"global fallback sync failed: {r.stderr}")
        global_bin_link = os.path.join(kugi_home, "bin", "shared")
        assert_true(os.path.islink(global_bin_link), "global bin symlink not created")

        xdg_data = os.path.join(root, "xdg_data")
        xdg_env = dict(base_env)
        del xdg_env["KUGI_HOME"]
        xdg_env["XDG_DATA_HOME"] = xdg_data
        r = run_kugi(kugi_bin, ["status"], xdg_env, cwd=empty_cwd)
        assert_eq(0, r.returncode, f"XDG_DATA_HOME status failed: {r.stderr}")
        assert_true(os.path.join(xdg_data, "kugi", "store") in r.stdout, r.stdout)

        # 10. Dead worktree GC (--dry-run and real) + stale tmp cleanup
        eph_wt = os.path.join(root, "wt_ephemeral")
        os.makedirs(eph_wt)
        eph_sha = hashlib.sha256(ephemeral_tar).hexdigest()
        with open(os.path.join(eph_wt, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                f'[tools.eph]\nversion = "1.0.0"\nurl = "{base_url}/downloads/ephemeral-1.0.0.tar.gz"\n'
                f'bin = ["eph"]\nsha256 = "{eph_sha}"\n'
            )
        r = run_kugi(kugi_bin, ["sync"], base_env, cwd=eph_wt)
        assert_eq(0, r.returncode, f"ephemeral sync failed: {r.stderr}")
        eph_key = tool_store_key("bin", eph_sha, ["eph:eph"])
        assert_true(os.path.isdir(os.path.join(kugi_home, "store", eph_key)))

        # Simulate deleting worktrees wt_1, wt_2, wt_3, and wt_ephemeral
        shutil.rmtree(eph_wt)
        for idx in (1, 2, 3):
            shutil.rmtree(worktrees[idx])

        # Add stale orphan file (>2h old) in tmp/
        stale_tmp = os.path.join(kugi_home, "tmp", "stale-orphan.part")
        with open(stale_tmp, "wb") as f:
            f.write(b"stale-bytes")
        old_time = time.time() - 7200
        os.utime(stale_tmp, (old_time, old_time))

        r = run_kugi(kugi_bin, ["gc", "--dry-run"], base_env, cwd=proj)
        assert_eq(0, r.returncode, f"gc --dry-run failed: {r.stderr}")
        assert_true("[dry-run] deleted_roots: 4" in r.stdout, r.stdout)
        assert_true("[dry-run] deleted_store_entries: 1" in r.stdout, r.stdout)
        assert_true(os.path.isdir(os.path.join(kugi_home, "store", eph_key)), "dry-run must not delete store entry")
        assert_true(os.path.isfile(stale_tmp), "dry-run must not delete stale tmp")

        r = run_kugi(kugi_bin, ["gc"], base_env, cwd=proj)
        assert_eq(0, r.returncode, f"gc failed: {r.stderr}")
        assert_true("deleted_roots: 4" in r.stdout, r.stdout)
        assert_true("deleted_store_entries: 1" in r.stdout, r.stdout)
        assert_true(not os.path.exists(os.path.join(kugi_home, "store", eph_key)), "dead store entry should be deleted")
        assert_true(not os.path.exists(stale_tmp), "stale tmp file should be deleted")
        assert_true(os.path.isdir(os.path.join(kugi_home, "store", shared_entries[0])), "live shared entry must survive GC")

        # 11. [store] max_size post-sync LRU eviction
        lru_old = os.path.join(kugi_home, "store", "lruold-1.0.0-aaaaaaaaaaaa")
        lru_new = os.path.join(kugi_home, "store", "lrunew-1.0.0-bbbbbbbbbbbb")
        os.makedirs(os.path.join(lru_old, "bin"))
        os.makedirs(os.path.join(lru_new, "bin"))
        with open(os.path.join(lru_old, "bin", "a"), "wb") as f:
            f.write(b"x" * 2048)
        with open(os.path.join(lru_new, "bin", "b"), "wb") as f:
            f.write(b"y" * 2048)
        os.utime(lru_old, (old_time, old_time))

        budget_dir = os.path.join(root, "budget_wt")
        os.makedirs(budget_dir)
        # Current live entries are tiny (< 2KB total); setting max_size = 3KB should evict lru_old while keeping lru_new
        with open(os.path.join(budget_dir, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                f'[store]\nmax_size = "3KB"\n\n'
                f'[tools.shared]\nversion = "1.0.0"\nurl = "{base_url}/downloads/shared-1.0.0.tar.gz"\n'
                f'bin = ["shared"]\nsha256 = "{shared_sha}"\n'
            )
        r = run_kugi(kugi_bin, ["sync"], base_env, cwd=budget_dir)
        assert_eq(0, r.returncode, f"budget sync failed: {r.stderr}")
        assert_true(not os.path.exists(lru_old), "LRU oldest unreferenced entry should be evicted")
        assert_true(os.path.exists(lru_new), "newer unreferenced entry should remain within max_size budget")

        # 12. `kugi lock --sync` single-download lock + store + bootstrap + links + roots + global bin
        oneshot_root = os.path.join(root, "oneshot_env")
        oneshot_home = os.path.join(oneshot_root, "home")
        oneshot_kugi_home = os.path.join(oneshot_root, "kugi_home")
        oneshot_xdg = os.path.join(oneshot_root, "xdg_config")
        oneshot_proj = os.path.join(oneshot_root, "proj")
        os.makedirs(oneshot_home)
        os.makedirs(oneshot_xdg)
        os.makedirs(oneshot_proj)
        oneshot_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": oneshot_home,
            "KUGI_HOME": oneshot_kugi_home,
            "XDG_CONFIG_HOME": oneshot_xdg,
            "TMPDIR": base_env["TMPDIR"],
        }
        oneshot_cfg_src = os.path.join(oneshot_home, "my-configs", "one.conf")
        os.makedirs(os.path.dirname(oneshot_cfg_src))
        with open(oneshot_cfg_src, "w", encoding="utf-8") as f:
            f.write("oneshot-cfg\n")
        with open(os.path.join(oneshot_proj, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                f'[tools.oneshot]\nversion = "1.0.0"\nurl = "{base_url}/downloads/oneshot-{{version}}.tar.gz"\n'
                f'bin = ["oneshot"]\n\n'
                f'[tools.oneboot]\nversion = "1.0.0"\nurl = "{base_url}/downloads/oneshot-boot-{{version}}"\n'
                f'archive = "none"\nmode = "bootstrap"\ninstall_dir = "~/.oneboot/bin"\nbin = ["oneboot"]\n\n'
                f'[links]\n"~/.config/one.conf" = "~/my-configs/one.conf"\n'
            )

        reg.reset_counts()
        r = run_kugi(kugi_bin, ["lock", "--sync"], oneshot_env, cwd=oneshot_proj)
        assert_eq(0, r.returncode, f"lock --sync failed: {r.stderr}")
        assert_eq(1, reg.count("/downloads/oneshot-1.0.0.tar.gz"), "lock --sync must download store tool only once")
        assert_eq(1, reg.count("/downloads/oneshot-boot-1.0.0"), "lock --sync must download bootstrap tool only once")
        assert_true(os.path.isfile(os.path.join(oneshot_proj, "kugi.lock")), "lock --sync must write kugi.lock")
        assert_eq([], os.listdir(os.path.join(oneshot_kugi_home, "tmp")), "tmp dir must be clean after lock --sync")
        assert_eq(1, len(os.listdir(os.path.join(oneshot_kugi_home, "roots"))), "lock --sync must register root")
        one_link = os.path.join(oneshot_home, ".config", "one.conf")
        assert_true(os.path.islink(one_link), "lock --sync must apply [links]")
        assert_eq(oneshot_cfg_src, os.readlink(one_link))
        r = run_kugi(
            kugi_bin,
            ["exec", "--", "sh", "-c", 'printf "%s|%s" "$(oneshot)" "$(oneboot)"'],
            oneshot_env,
            cwd=oneshot_proj,
        )
        assert_eq(0, r.returncode, f"exec after lock --sync failed: {r.stderr}")
        assert_eq("oneshot-ok|oneshot-boot-ok", r.stdout)

        # Follow-up sync after lock --sync must perform zero downloads
        reg.reset_counts()
        r = run_kugi(kugi_bin, ["sync"], oneshot_env, cwd=oneshot_proj)
        assert_eq(0, r.returncode, f"follow-up sync failed: {r.stderr}")
        assert_eq(0, reg.count("/downloads/oneshot-1.0.0.tar.gz"), "follow-up sync must not re-download store tool")
        assert_eq(0, reg.count("/downloads/oneshot-boot-1.0.0"), "follow-up sync must not re-download bootstrap tool")

        # Global manifest lock --sync must also create KUGI_HOME/bin/ symlinks in a single download
        oneshot_global_dir = os.path.join(oneshot_xdg, "kugi")
        os.makedirs(oneshot_global_dir)
        with open(os.path.join(oneshot_global_dir, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                f'[tools.globtool]\nversion = "1.0.0"\nurl = "{base_url}/downloads/globtool-{{version}}.tar.gz"\n'
                f'bin = ["globtool"]\n'
            )
        oneshot_empty = os.path.join(oneshot_root, "empty")
        os.makedirs(oneshot_empty)
        reg.reset_counts()
        r = run_kugi(kugi_bin, ["lock", "--sync"], oneshot_env, cwd=oneshot_empty)
        assert_eq(0, r.returncode, f"global lock --sync failed: {r.stderr}")
        assert_eq(1, reg.count("/downloads/globtool-1.0.0.tar.gz"), "global lock --sync must download only once")
        assert_true(os.path.islink(os.path.join(oneshot_kugi_home, "bin", "globtool")), "global lock --sync must link global bin")

        # 13. [assets.<name>] lock --sync -> {asset.*} / {tool.*} in exec & env -> status -> dead-worktree GC
        asset_root = os.path.join(root, "asset_env")
        asset_home = os.path.join(asset_root, "home")
        asset_kugi_home = os.path.join(asset_root, "kugi_home")
        asset_xdg = os.path.join(asset_root, "xdg_config")
        asset_wt = os.path.join(asset_root, "wt_assets")
        os.makedirs(asset_home)
        os.makedirs(asset_xdg)
        os.makedirs(asset_wt)
        asset_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": asset_home,
            "KUGI_HOME": asset_kugi_home,
            "XDG_CONFIG_HOME": asset_xdg,
            "TMPDIR": base_env["TMPDIR"],
        }

        # Tool-only lockfile omits "assets" for backward compatibility
        assert_true("assets" not in lock_json, "tool-only kugi.lock must omit assets key")

        # bin field in [assets.<name>] must fail validation with exit 2
        bad_asset_dir = os.path.join(asset_root, "bad_asset")
        os.makedirs(bad_asset_dir)
        with open(os.path.join(bad_asset_dir, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                f'[assets.fonts]\nversion = "1.0.0"\nurl = "{base_url}/downloads/fonts-1.0.0.tar.gz"\n'
                f'bin = ["fonts"]\n'
            )
        r = run_kugi(kugi_bin, ["lock"], asset_env, cwd=bad_asset_dir)
        assert_eq(2, r.returncode, "bin field inside [assets.<name>] must exit 2")

        with open(os.path.join(asset_wt, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                f'[tools.alpha]\nversion = "1.0.0"\nurl = "{base_url}/downloads/alpha-{{version}}.tar.gz"\n'
                f'bin = ["alpha"]\n\n'
                f'[tools.boot]\nversion = "1.0.0"\nurl = "{base_url}/downloads/boot-{{version}}"\n'
                f'archive = "none"\nmode = "bootstrap"\ninstall_dir = "~/.asset-boot/bin"\nbin = ["boot"]\n\n'
                f'[assets.fonts]\nversion = "1.0.0"\nurl = "{base_url}/downloads/fonts-{{version}}.tar.gz"\n\n'
                f'[assets.bundle]\nversion = "1.0.0"\nurl = "{base_url}/downloads/bundle-{{version}}.tar.gz"\n'
                f'unwrap = false\n\n'
                f'[assets.dict]\nversion = "1.0.0"\nurl = "{base_url}/downloads/dict-{{version}}.dat"\n'
                f'archive = "none"\n\n'
                f'[env]\n'
                f'FONT_DIR = "{{asset.fonts}}"\n'
                f'FONT_FILE = "{{asset.fonts}}/otf/regular.otf"\n'
                f'BUNDLE_FILE = "{{asset.bundle}}/bundle-1.0.0/meta.txt"\n'
                f'DICT_FILE = "{{asset.dict}}/dict-1.0.0.dat"\n'
                f'TOOL_DIR = "{{tool.alpha}}"\n'
                f'BOOT_DIR = "{{tool.boot}}"\n'
                f'COMBO = "~/cache:{{manifest_dir}}/cfg:{{asset.fonts}}:{{tool.alpha}}"\n'
            )

        reg.reset_counts()
        r = run_kugi(kugi_bin, ["lock", "--sync"], asset_env, cwd=asset_wt)
        assert_eq(0, r.returncode, f"asset lock --sync failed: {r.stderr}")
        assert_eq(1, reg.count("/downloads/alpha-1.0.0.tar.gz"), "alpha downloaded once")
        assert_eq(1, reg.count("/downloads/fonts-1.0.0.tar.gz"), "fonts downloaded once")
        assert_eq(1, reg.count("/downloads/bundle-1.0.0.tar.gz"), "bundle downloaded once")
        assert_eq(1, reg.count("/downloads/dict-1.0.0.dat"), "dict downloaded once")
        assert_eq([], os.listdir(os.path.join(asset_kugi_home, "tmp")), "tmp clean after asset lock --sync")

        with open(os.path.join(asset_wt, "kugi.lock"), "r", encoding="utf-8") as f:
            asset_lock = json.loads(f.read())
        assert_eq(["bundle", "dict", "fonts"], list(asset_lock["assets"].keys()), "sorted lock asset keys")

        fonts_sha = hashlib.sha256(fonts_tar).hexdigest()
        bundle_sha = hashlib.sha256(bundle_tar).hexdigest()
        dict_sha = hashlib.sha256(dict_raw).hexdigest()
        alpha_sha = hashlib.sha256(strip_tar).hexdigest()

        fonts_store = os.path.join(asset_kugi_home, "store", asset_store_key(fonts_sha, unwrap=True))
        bundle_store = os.path.join(asset_kugi_home, "store", asset_store_key(bundle_sha, unwrap=False))
        dict_store = os.path.join(asset_kugi_home, "store", asset_store_key(dict_sha, raw_filename="dict-1.0.0.dat"))
        alpha_store = os.path.join(asset_kugi_home, "store", tool_store_key("bin", alpha_sha, ["alpha:alpha"]))
        assert_true(os.path.isfile(os.path.join(fonts_store, "otf", "regular.otf")), "unwrapped fonts file missing")
        assert_true(not os.path.exists(os.path.join(fonts_store, "fonts-1.0.0")), "fonts top dir must be unwrapped")
        assert_true(os.path.isfile(os.path.join(bundle_store, "bundle-1.0.0", "meta.txt")), "unwrap=false must preserve top dir")
        assert_true(os.path.isfile(os.path.join(dict_store, "dict-1.0.0.dat")), "single-file asset missing")

        # kugi status reports assets
        r = run_kugi(kugi_bin, ["status"], asset_env, cwd=asset_wt)
        assert_eq(0, r.returncode, f"asset status failed: {r.stderr}")
        assert_true(f"asset:fonts 1.0.0 ok {fonts_store}" in r.stdout, r.stdout)
        assert_true(f"asset:bundle 1.0.0 ok {bundle_store}" in r.stdout, r.stdout)
        assert_true(f"asset:dict 1.0.0 ok {dict_store}" in r.stdout, r.stdout)

        # kugi exec expands {asset.<name>} and {tool.<name>} (store + bootstrap)
        asset_wt_real = os.path.realpath(asset_wt)
        r = run_kugi(
            kugi_bin,
            [
                "exec",
                "--",
                "sh",
                "-c",
                'printf "%s|%s|%s|%s|%s|%s|%s" "$(cat "$FONT_FILE")" "$(cat "$BUNDLE_FILE")" "$(cat "$DICT_FILE")" "$TOOL_DIR" "$BOOT_DIR" "$COMBO" "$(alpha)"',
            ],
            asset_env,
            cwd=asset_wt,
        )
        assert_eq(0, r.returncode, f"asset exec failed: {r.stderr}")
        assert_eq(
            f"font-otf-bytes|bundle-meta|dict-raw-payload|{alpha_store}|{asset_home}/.asset-boot/bin|{asset_home}/cache:{asset_wt_real}/cfg:{fonts_store}:{alpha_store}|alpha-v1",
            r.stdout,
            "exec placeholder expansion mismatch",
        )

        # kugi env expands {asset.<name>} and {tool.<name>} for sh and fish
        r = run_kugi(kugi_bin, ["env", "--shell", "sh"], asset_env, cwd=asset_wt)
        assert_eq(0, r.returncode, f"asset env sh failed: {r.stderr}")
        assert_true(f"export FONT_DIR='{fonts_store}'" in r.stdout, r.stdout)
        assert_true(f"export TOOL_DIR='{alpha_store}'" in r.stdout, r.stdout)
        assert_true(f"export BOOT_DIR='{asset_home}/.asset-boot/bin'" in r.stdout, r.stdout)
        r = run_kugi(kugi_bin, ["env", "--shell", "fish"], asset_env, cwd=asset_wt)
        assert_eq(0, r.returncode, f"asset env fish failed: {r.stderr}")
        assert_true(f"set -gx FONT_DIR '{fonts_store}'" in r.stdout, r.stdout)
        assert_true(f"set -gx TOOL_DIR '{alpha_store}'" in r.stdout, r.stdout)

        # Unknown {asset.<name>} or {tool.<name>} in [env] fails clearly on exec and env
        bad_env_dir = os.path.join(asset_root, "bad_env_ref")
        os.makedirs(bad_env_dir)
        with open(os.path.join(bad_env_dir, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write('[env]\nBROKEN = "{asset.missing}"\n')
        for subcmd in (["env"], ["exec", "--", "true"]):
            r = run_kugi(kugi_bin, subcmd, asset_env, cwd=bad_env_dir)
            assert_eq(2, r.returncode, f"{subcmd} unknown asset must exit 2")
            assert_true("unknown asset 'missing'" in r.stderr, r.stderr)

        with open(os.path.join(bad_env_dir, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write('[env]\nBROKEN = "{tool.nope}"\n')
        for subcmd in (["env"], ["exec", "--", "true"]):
            r = run_kugi(kugi_bin, subcmd, asset_env, cwd=bad_env_dir)
            assert_eq(2, r.returncode, f"{subcmd} unknown tool must exit 2")
            assert_true("unknown tool 'nope'" in r.stderr, r.stderr)

        # Asset SHA-256 mismatch and path traversal rejections
        bad_asset_sec = os.path.join(asset_root, "bad_asset_sec")
        os.makedirs(bad_asset_sec)
        with open(os.path.join(bad_asset_sec, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                f'[assets.tampered]\nversion = "1.0.0"\nurl = "{base_url}/downloads/dict-1.0.0.dat"\n'
                f'archive = "none"\nsha256 = "0000000000000000000000000000000000000000000000000000000000000000"\n'
            )
        r = run_kugi(kugi_bin, ["sync"], asset_env, cwd=bad_asset_sec)
        assert_eq(1, r.returncode, "asset sha256 mismatch must fail")
        assert_true("sha256 mismatch" in r.stderr, r.stderr)

        trav_sha = hashlib.sha256(traversal_tar).hexdigest()
        with open(os.path.join(bad_asset_sec, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                f'[assets.evil]\nversion = "1.0.0"\nurl = "{base_url}/downloads/traversal.tar.gz"\n'
                f'sha256 = "{trav_sha}"\n'
            )
        r = run_kugi(kugi_bin, ["sync"], asset_env, cwd=bad_asset_sec)
        assert_eq(1, r.returncode, "asset traversal archive must fail")

        # Dead worktree GC reclaims asset store entries (and tool entries)
        shutil.rmtree(asset_wt)
        r = run_kugi(kugi_bin, ["gc", "--dry-run"], asset_env, cwd=asset_root)
        assert_eq(0, r.returncode, f"asset gc --dry-run failed: {r.stderr}")
        assert_true("[dry-run] deleted_roots: 1" in r.stdout, r.stdout)
        assert_true("[dry-run] deleted_store_entries: 4" in r.stdout, r.stdout)
        assert_true(os.path.isdir(fonts_store), "dry-run must keep fonts store")

        r = run_kugi(kugi_bin, ["gc"], asset_env, cwd=asset_root)
        assert_eq(0, r.returncode, f"asset gc failed: {r.stderr}")
        assert_true("deleted_roots: 1" in r.stdout, r.stdout)
        assert_true("deleted_store_entries: 4" in r.stdout, r.stdout)
        assert_true(not os.path.exists(fonts_store), "dead asset fonts must be reclaimed")
        assert_true(not os.path.exists(bundle_store), "dead asset bundle must be reclaimed")
        assert_true(not os.path.exists(dict_store), "dead asset dict must be reclaimed")
        assert_true(not os.path.exists(alpha_store), "dead tool alpha must be reclaimed")

        # 14. Multi-target `kugi lock --target darwin-aarch64 --target linux-x86_64` + merge + `--sync` + `--asset`
        mt_root = os.path.join(root, "mt_env")
        mt_home = os.path.join(mt_root, "home")
        mt_kugi_home = os.path.join(mt_root, "kugi_home")
        mt_xdg = os.path.join(mt_root, "xdg_config")
        mt_proj = os.path.join(mt_root, "proj")
        os.makedirs(mt_home)
        os.makedirs(mt_xdg)
        os.makedirs(mt_proj)
        mt_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": mt_home,
            "KUGI_HOME": mt_kugi_home,
            "XDG_CONFIG_HOME": mt_xdg,
            "TMPDIR": base_env["TMPDIR"],
        }
        with open(os.path.join(mt_proj, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                f'[tools.mtool]\nversion = "1.0.0"\n'
                f'url = "{base_url}/multi/mtool-{{version}}-{{os}}-{{arch}}.tar.gz"\n'
                f'bin = ["mtool"]\n\n'
                f'[assets.mpack]\nversion = "1.0.0"\n'
                f'url = "{base_url}/multi/mpack-{{version}}-{{os}}-{{arch}}.tar.gz"\n\n'
                f'[env]\nMPACK_INFO = "{{asset.mpack}}/info.txt"\n'
            )

        # Invalid --target exits 2
        r = run_kugi(kugi_bin, ["lock", "--target", "invalid-target"], mt_env, cwd=mt_proj)
        assert_eq(2, r.returncode, "invalid --target must exit 2")

        # Pre-seed linux-aarch64 in kugi.lock to verify merge preserves existing targets
        r = run_kugi(kugi_bin, ["lock", "--target", "linux-aarch64"], mt_env, cwd=mt_proj)
        assert_eq(0, r.returncode, f"lock linux-aarch64 failed: {r.stderr}")

        # Lock darwin-aarch64 + linux-x86_64 (preserving linux-aarch64)
        reg.reset_counts()
        r = run_kugi(
            kugi_bin,
            ["lock", "--target", "darwin-aarch64", "--target", "linux-x86_64"],
            mt_env,
            cwd=mt_proj,
        )
        assert_eq(0, r.returncode, f"multi-target lock failed: {r.stderr}")
        with open(os.path.join(mt_proj, "kugi.lock"), "r", encoding="utf-8") as f:
            mt_lock = json.loads(f.read())
        assert_eq(
            ["darwin-aarch64", "linux-aarch64", "linux-x86_64"],
            list(mt_lock["tools"]["mtool"]["targets"].keys()),
            "tool targets must merge and stay sorted",
        )
        assert_eq(
            ["darwin-aarch64", "linux-aarch64", "linux-x86_64"],
            list(mt_lock["assets"]["mpack"]["targets"].keys()),
            "asset targets must merge and stay sorted",
        )
        for tgt in ("darwin-aarch64", "linux-aarch64", "linux-x86_64"):
            exp_tool_sha = hashlib.sha256(multi_payloads[("tool", tgt)]).hexdigest()
            exp_pack_sha = hashlib.sha256(multi_payloads[("pack", tgt)]).hexdigest()
            assert_eq(exp_tool_sha, mt_lock["tools"]["mtool"]["targets"][tgt]["sha256"], f"tool sha {tgt}")
            assert_eq(exp_pack_sha, mt_lock["assets"]["mpack"]["targets"][tgt]["sha256"], f"asset sha {tgt}")
        assert_eq([], os.listdir(os.path.join(mt_kugi_home, "store")), "plain multi-target lock must not populate store")

        # Selective `kugi lock --asset mpack --target linux-x86_64` skips tool downloads
        reg.reset_counts()
        r = run_kugi(kugi_bin, ["lock", "--asset", "mpack", "--target", "linux-x86_64"], mt_env, cwd=mt_proj)
        assert_eq(0, r.returncode, f"selective asset lock failed: {r.stderr}")
        assert_eq(0, reg.count("/multi/mtool-1.0.0-linux-x86_64.tar.gz"), "selective --asset must not download tool")
        assert_eq(1, reg.count("/multi/mpack-1.0.0-linux-x86_64.tar.gz"), "selective --asset must download asset")

        # `kugi lock --target <other> --target <host> --sync` locks both, downloads each once, and stages only host locally
        os.unlink(os.path.join(mt_proj, "kugi.lock"))
        reg.reset_counts()
        r = run_kugi(
            kugi_bin,
            ["lock", "--target", other_target, "--target", host_target, "--sync"],
            mt_env,
            cwd=mt_proj,
        )
        assert_eq(0, r.returncode, f"multi-target lock --sync failed: {r.stderr}")
        assert_eq(1, reg.count(f"/multi/mtool-1.0.0-{other_target}.tar.gz"), "other tool target downloaded once")
        assert_eq(1, reg.count(f"/multi/mtool-1.0.0-{host_target}.tar.gz"), "host tool target downloaded once (reused)")
        assert_eq(1, reg.count(f"/multi/mpack-1.0.0-{other_target}.tar.gz"), "other asset target downloaded once")
        assert_eq(1, reg.count(f"/multi/mpack-1.0.0-{host_target}.tar.gz"), "host asset target downloaded once (reused)")
        assert_eq([], os.listdir(os.path.join(mt_kugi_home, "tmp")), "tmp clean after multi-target lock --sync")

        host_tool_sha = hashlib.sha256(multi_payloads[("tool", host_target)]).hexdigest()
        host_pack_sha = hashlib.sha256(multi_payloads[("pack", host_target)]).hexdigest()
        other_tool_sha = hashlib.sha256(multi_payloads[("tool", other_target)]).hexdigest()
        other_pack_sha = hashlib.sha256(multi_payloads[("pack", other_target)]).hexdigest()
        host_tool_key = tool_store_key("bin", host_tool_sha, ["mtool:mtool"])
        host_pack_key = asset_store_key(host_pack_sha, unwrap=True)
        other_tool_key = tool_store_key("bin", other_tool_sha, ["mtool:mtool"])
        other_pack_key = asset_store_key(other_pack_sha, unwrap=True)
        mt_store_entries = sorted(os.listdir(os.path.join(mt_kugi_home, "store")))
        assert_eq(
            sorted([host_tool_key, host_pack_key]),
            mt_store_entries,
            "only host target entries may be staged locally",
        )
        assert_true(other_tool_key not in mt_store_entries)
        assert_true(other_pack_key not in mt_store_entries)

        r = run_kugi(
            kugi_bin,
            ["exec", "--", "sh", "-c", 'printf "%s|%s" "$(mtool)" "$(cat "$MPACK_INFO")"'],
            mt_env,
            cwd=mt_proj,
        )
        assert_eq(0, r.returncode, f"exec after multi-target lock --sync failed: {r.stderr}")
        assert_eq(f"mtool-{host_target}|pack-{host_target}", r.stdout)

        reg.reset_counts()
        r = run_kugi(kugi_bin, ["sync"], mt_env, cwd=mt_proj)
        assert_eq(0, r.returncode, f"follow-up sync failed: {r.stderr}")
        assert_eq(0, reg.count(f"/multi/mtool-1.0.0-{host_target}.tar.gz"), "follow-up sync must do 0 downloads")
        assert_eq(0, reg.count(f"/multi/mpack-1.0.0-{host_target}.tar.gz"), "follow-up sync must do 0 downloads")

        # 15. Download error diagnostics + TLS 1.3 primary vs TLS 1.2 curl fallback
        reg.error("/err/403", 403, b"forbidden")
        reg.error("/err/500", 500, b"server error")
        reg.redirect("/err/loop", "/err/loop")

        diag_root = os.path.join(root, "diag_env")
        diag_home = os.path.join(diag_root, "home")
        diag_kugi_home = os.path.join(diag_root, "kugi_home")
        diag_xdg = os.path.join(diag_root, "xdg_config")
        diag_proj = os.path.join(diag_root, "proj")
        os.makedirs(diag_home)
        os.makedirs(diag_xdg)
        os.makedirs(diag_proj)
        diag_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": diag_home,
            "KUGI_HOME": diag_kugi_home,
            "XDG_CONFIG_HOME": diag_xdg,
            "TMPDIR": base_env["TMPDIR"],
        }

        for case_name, bad_url, expected_reason in [
            ("err404", f"{base_url}/err/missing", "HTTP 404"),
            ("err403", f"{base_url}/err/403", "HTTP 403"),
            ("err500", f"{base_url}/err/500", "HTTP 500"),
            ("errloop", f"{base_url}/err/loop", "too many redirects"),
            ("errnet", "http://127.0.0.1:1/unreachable", "TLS / network error"),
        ]:
            with open(os.path.join(diag_proj, "kugi.toml"), "w", encoding="utf-8") as f:
                f.write(
                    f'[tools.{case_name}]\nversion = "1.0.0"\nurl = "{bad_url}"\n'
                    f'archive = "none"\nbin = ["{case_name}"]\n'
                )
            r = run_kugi(kugi_bin, ["lock"], diag_env, cwd=diag_proj)
            assert_eq(1, r.returncode, f"{case_name} must exit 1")
            assert_true(
                f"failed to download '{case_name}'" in r.stderr and expected_reason in r.stderr,
                f"{case_name}: expected {expected_reason!r} in stderr, got: {r.stderr!r}",
            )

        # Missing/empty SSL_CERT_FILE reports no trusted CA certificates found
        no_ca_env = dict(diag_env)
        no_ca_env["SSL_CERT_FILE"] = os.path.join(diag_root, "missing-ca.pem")
        with open(os.path.join(diag_proj, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                '[tools.noca]\nversion = "1.0.0"\nurl = "https://localhost:1/artifact"\n'
                'archive = "none"\nbin = ["noca"]\n'
            )
        r = run_kugi(kugi_bin, ["lock"], no_ca_env, cwd=diag_proj)
        assert_eq(1, r.returncode, "missing CA bundle must exit 1")
        assert_true(
            "no trusted CA certificates found" in r.stderr,
            f"expected no trusted CA diagnostic, got: {r.stderr!r}",
        )

        # Loopback HTTPS tests (TLS 1.3 native vs TLS 1.2 curl fallback)
        cert_dir = os.path.join(diag_root, "certs")
        os.makedirs(cert_dir)
        cert_path, key_path = generate_self_signed_cert(cert_dir, "srv")
        untrusted_cert, _ = generate_self_signed_cert(cert_dir, "untrusted")

        tls13_tar = make_tar_gz([
            {"name": "tls13tool-1.0.0/tls13tool", "data": b"#!/bin/sh\necho tls13-ok\n"},
        ])
        tls12_tar = make_tar_gz([
            {"name": "tls12tool-1.0.0/tls12tool", "data": b"#!/bin/sh\necho tls12-ok\n"},
        ])

        tls13_reg = MockRegistry()
        tls13_reg.put("/downloads/tls13tool-1.0.0.tar.gz", tls13_tar)
        tls13_reg.redirect("/downgrade", f"{base_url}/downloads/alpha-1.0.0.tar.gz")
        tls13_srv, tls13_port = start_mock_https_server(
            tls13_reg, cert_path, key_path, ssl.TLSVersion.TLSv1_3
        )

        tls12_reg = MockRegistry()
        tls12_reg.put("/downloads/tls12tool-1.0.0.tar.gz", tls12_tar)
        tls12_reg.redirect("/redir/tls12tool-1.0.0.tar.gz", "/downloads/tls12tool-1.0.0.tar.gz")
        tls12_reg.redirect("/downgrade", f"{base_url}/downloads/alpha-1.0.0.tar.gz")
        tls12_reg.redirect("/loop", "/loop")
        tls12_reg.error("/err/500", 500, b"boom")
        tls12_srv, tls12_port = start_mock_https_server(
            tls12_reg, cert_path, key_path, ssl.TLSVersion.TLSv1_2
        )
        tls13_reg.redirect("/to-tls12", f"https://localhost:{tls12_port}/downloads/tls12tool-1.0.0.tar.gz")

        try:
            real_curl = shutil.which("curl")
            assert_true(real_curl is not None, "system curl must exist on PATH")
            spy_dir = os.path.join(diag_root, "spy_bin")
            os.makedirs(spy_dir)
            spy_log = os.path.join(diag_root, "curl-spy.log")
            spy_curl = os.path.join(spy_dir, "curl")
            with open(spy_curl, "w", encoding="utf-8") as f:
                f.write(
                    f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{spy_log}"\nexec "{real_curl}" "$@"\n'
                )
            os.chmod(spy_curl, 0o755)

            tls_env = dict(diag_env)
            tls_env["PATH"] = spy_dir + os.pathsep + diag_env["PATH"]
            tls_env["SSL_CERT_FILE"] = cert_path

            # Native TLS 1.3 path succeeds without invoking curl
            tls13_proj = os.path.join(diag_root, "tls13_proj")
            os.makedirs(tls13_proj)
            with open(os.path.join(tls13_proj, "kugi.toml"), "w", encoding="utf-8") as f:
                f.write(
                    f'[tools.tls13tool]\nversion = "1.0.0"\n'
                    f'url = "https://localhost:{tls13_port}/downloads/tls13tool-{{version}}.tar.gz"\n'
                    f'bin = ["tls13tool"]\n'
                )
            r = run_kugi(kugi_bin, ["lock", "--sync"], tls_env, cwd=tls13_proj)
            assert_eq(0, r.returncode, f"TLS 1.3 native lock --sync failed: {r.stderr}")
            assert_true(not os.path.exists(spy_log), "TLS 1.3 primary path must not invoke curl")
            r = run_kugi(kugi_bin, ["exec", "--", "tls13tool"], tls_env, cwd=tls13_proj)
            assert_eq(0, r.returncode, f"TLS 1.3 exec failed: {r.stderr}")
            assert_eq("tls13-ok", r.stdout.strip())

            # Native TLS 1.3 rejects https -> http downgrade without calling curl
            with open(os.path.join(tls13_proj, "kugi.toml"), "w", encoding="utf-8") as f:
                f.write(
                    f'[tools.downg]\nversion = "1.0.0"\n'
                    f'url = "https://localhost:{tls13_port}/downgrade"\n'
                    f'bin = ["alpha"]\n'
                )
            r = run_kugi(kugi_bin, ["lock"], tls_env, cwd=tls13_proj)
            assert_eq(1, r.returncode, "TLS 1.3 https -> http downgrade must exit 1")
            assert_true(
                "insecure redirect (https -> http)" in r.stderr,
                f"expected insecure redirect diagnostic, got: {r.stderr!r}",
            )
            assert_true(not os.path.exists(spy_log), "insecure redirect must not fall back to curl")

            # TLS 1.2-only host falls back to HTTPS-only curl and computes SHA-256 in Kizu
            tls12_proj = os.path.join(diag_root, "tls12_proj")
            os.makedirs(tls12_proj)
            with open(os.path.join(tls12_proj, "kugi.toml"), "w", encoding="utf-8") as f:
                f.write(
                    f'[tools.tls12tool]\nversion = "1.0.0"\n'
                    f'url = "https://localhost:{tls12_port}/redir/tls12tool-{{version}}.tar.gz"\n'
                    f'bin = ["tls12tool"]\n'
                )
            r = run_kugi(kugi_bin, ["lock", "--sync"], tls_env, cwd=tls12_proj)
            assert_eq(0, r.returncode, f"TLS 1.2 fallback lock --sync failed: {r.stderr}")
            assert_true(os.path.isfile(spy_log), "TLS 1.2 host must trigger curl fallback")
            with open(spy_log, "r", encoding="utf-8") as f:
                spy_calls = f.read()
            assert_true("--proto =https --proto-redir =https" in spy_calls, spy_calls)
            with open(os.path.join(tls12_proj, "kugi.lock"), "r", encoding="utf-8") as f:
                tls12_lock = json.loads(f.read())
            exp_tls12_sha = hashlib.sha256(tls12_tar).hexdigest()
            assert_eq(
                exp_tls12_sha,
                tls12_lock["tools"]["tls12tool"]["targets"][host_target]["sha256"],
                "TLS 1.2 fallback SHA-256 mismatch",
            )
            assert_eq([], os.listdir(os.path.join(diag_kugi_home, "tmp")), "tmp clean after TLS 1.2 fallback")
            r = run_kugi(kugi_bin, ["exec", "--", "tls12tool"], tls_env, cwd=tls12_proj)
            assert_eq(0, r.returncode, f"TLS 1.2 exec failed: {r.stderr}")
            assert_eq("tls12-ok", r.stdout.strip())

            # TLS 1.3 redirect into TLS 1.2 host falls back mid-chain with decremented --max-redirs
            os.unlink(spy_log)
            with open(os.path.join(diag_proj, "kugi.toml"), "w", encoding="utf-8") as f:
                f.write(
                    f'[tools.cross]\nversion = "1.0.0"\n'
                    f'url = "https://localhost:{tls13_port}/to-tls12"\n'
                    f'bin = ["tls12tool"]\n'
                )
            r = run_kugi(kugi_bin, ["lock"], tls_env, cwd=diag_proj)
            assert_eq(0, r.returncode, f"TLS 1.3 -> TLS 1.2 redirect lock failed: {r.stderr}")
            with open(spy_log, "r", encoding="utf-8") as f:
                cross_calls = f.read()
            assert_true("--max-redirs 9" in cross_calls, cross_calls)

            # TLS 1.2 fallback still enforces https -> http downgrade rejection, redirect limit, and HTTP status diagnostics
            for case_name, path_part, expected_reason in [
                ("t12downgrade", "/downgrade", "insecure redirect (https -> http)"),
                ("t12loop", "/loop", "too many redirects"),
                ("t12notfound", "/missing", "HTTP 404"),
                ("t12err500", "/err/500", "HTTP 500"),
            ]:
                with open(os.path.join(diag_proj, "kugi.toml"), "w", encoding="utf-8") as f:
                    f.write(
                        f'[tools.{case_name}]\nversion = "1.0.0"\n'
                        f'url = "https://localhost:{tls12_port}{path_part}"\n'
                        f'bin = ["{case_name}"]\n'
                    )
                r = run_kugi(kugi_bin, ["lock"], tls_env, cwd=diag_proj)
                assert_eq(1, r.returncode, f"{case_name} must exit 1")
                assert_true(
                    expected_reason in r.stderr,
                    f"{case_name}: expected {expected_reason!r} in stderr, got: {r.stderr!r}",
                )
                assert_eq([], os.listdir(os.path.join(diag_kugi_home, "tmp")), f"tmp clean after {case_name}")

            # TLS 1.2 fallback rejects SHA-256 mismatch before extraction
            t12_tamper = os.path.join(diag_root, "t12_tamper")
            os.makedirs(t12_tamper)
            with open(os.path.join(t12_tamper, "kugi.toml"), "w", encoding="utf-8") as f:
                f.write(
                    f'[tools.t12bad]\nversion = "1.0.0"\n'
                    f'url = "https://localhost:{tls12_port}/downloads/tls12tool-1.0.0.tar.gz"\n'
                    f'bin = ["tls12tool"]\n'
                    f'sha256 = "0000000000000000000000000000000000000000000000000000000000000000"\n'
                )
            r = run_kugi(kugi_bin, ["sync"], tls_env, cwd=t12_tamper)
            assert_eq(1, r.returncode, "TLS 1.2 fallback sha256 mismatch must fail")
            assert_true("sha256 mismatch" in r.stderr, r.stderr)
            assert_eq([], [e for e in os.listdir(os.path.join(diag_kugi_home, "store")) if e.startswith("tool-bin-0000000000000000-")])
            assert_eq([], os.listdir(os.path.join(diag_kugi_home, "tmp")), "tmp clean after TLS 1.2 hash mismatch")

            # TLS 1.2 fallback rejects untrusted certificates
            untrusted_env = dict(tls_env)
            untrusted_env["SSL_CERT_FILE"] = untrusted_cert
            with open(os.path.join(diag_proj, "kugi.toml"), "w", encoding="utf-8") as f:
                f.write(
                    f'[tools.untrusted]\nversion = "1.0.0"\n'
                    f'url = "https://localhost:{tls12_port}/downloads/tls12tool-1.0.0.tar.gz"\n'
                    f'bin = ["tls12tool"]\n'
                )
            r = run_kugi(kugi_bin, ["lock"], untrusted_env, cwd=diag_proj)
            assert_eq(1, r.returncode, "untrusted CA cert must fail")
            assert_true("TLS / network error" in r.stderr, f"expected TLS / network error, got: {r.stderr!r}")
            assert_eq([], os.listdir(os.path.join(diag_kugi_home, "tmp")), "tmp clean after untrusted cert")
        finally:
            tls13_srv.shutdown()
            tls12_srv.shutdown()

        # 16. Standalone .tgz + [wrappers.<name>] + [env_prepend] + cross-project deduplication & GC
        npm_tgz = make_tar_gz([
            {
                "name": "package/dist/cli.sh",
                "data": b'printf "%s|%s|%s|%s|%s|%s" "npm-tgz-ok" "$NODE_ENV" "$NODE_PATH" "${WRAPPED_SELF:-no}" "$1" "$2"\n',
                "mode": 0o644,
            },
            {
                "name": "package/lib/pkgconfig/demo.pc",
                "data": b"Name: demo\n",
                "mode": 0o644,
            },
        ])
        sdk_a_tgz = make_tar_gz([
            {"name": "sdk-a-1.0.0/lib/pkgconfig/a.pc", "data": b"Name: a\n", "mode": 0o644},
            {"name": "sdk-a-1.0.0/include/a.h", "data": b"int a(void);\n", "mode": 0o644},
        ])
        sdk_b_tgz = make_tar_gz([
            {"name": "sdk-b-1.0.0/lib/pkgconfig/b.pc", "data": b"Name: b\n", "mode": 0o644},
        ])
        jsrt_tar = make_tar_gz([
            {
                "name": "jsrt-1.0.0/bin/jsrt",
                "data": b'#!/bin/sh\nexec /bin/sh "$@"\n',
            },
        ])
        reg.put("/downloads/npm-cli-1.0.0.tgz", npm_tgz)
        reg.put("/downloads/sdk-a-1.0.0.tgz", sdk_a_tgz)
        reg.put("/downloads/sdk-b-1.0.0.tgz", sdk_b_tgz)
        reg.put("/downloads/jsrt-1.0.0.tar.gz", jsrt_tar)

        wrap_root = os.path.join(root, "wrap_env")
        wrap_home = os.path.join(wrap_root, "home")
        wrap_kugi_home = os.path.join(wrap_root, "kugi_home")
        wrap_xdg = os.path.join(wrap_root, "xdg_config")
        wrap_proj = os.path.join(wrap_root, "proj")
        os.makedirs(wrap_home)
        os.makedirs(wrap_xdg)
        os.makedirs(wrap_proj)
        wrap_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": wrap_home,
            "KUGI_HOME": wrap_kugi_home,
            "XDG_CONFIG_HOME": wrap_xdg,
            "TMPDIR": base_env["TMPDIR"],
        }

        with open(os.path.join(wrap_proj, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                f'[tools.jsrt]\nversion = "1.0.0"\nurl = "{base_url}/downloads/jsrt-{{version}}.tar.gz"\n'
                f'bin = ["jsrt"]\n\n'
                f'[assets.npm_cli]\nversion = "1.0.0"\nurl = "{base_url}/downloads/npm-cli-{{version}}.tgz"\n\n'
                f'[assets.sdk_a]\nversion = "1.0.0"\nurl = "{base_url}/downloads/sdk-a-{{version}}.tgz"\n\n'
                f'[assets.sdk_b]\nversion = "1.0.0"\nurl = "{base_url}/downloads/sdk-b-{{version}}.tgz"\n\n'
                f'[wrappers.npm-cli]\n'
                f'exec = "{{tool.jsrt}}/bin/jsrt"\n'
                f'args = ["{{asset.npm_cli}}/dist/cli.sh"]\n'
                f'env = {{ NODE_ENV = "production" }}\n'
                f'env_prepend = {{ NODE_PATH = "{{asset.npm_cli}}/node_modules" }}\n\n'
                f'[wrappers.jsrt]\n'
                f'exec = "{{tool.jsrt}}/bin/jsrt"\n'
                f'env = {{ WRAPPED_SELF = "yes" }}\n\n'
                f'[env_prepend]\n'
                f'PKG_CONFIG_PATH = ["{{asset.sdk_a}}/lib/pkgconfig", "{{asset.sdk_b}}/lib/pkgconfig"]\n'
                f'CPATH = "{{asset.sdk_a}}/include"\n'
            )

        reg.reset_counts()
        r = run_kugi(kugi_bin, ["lock", "--sync"], wrap_env, cwd=wrap_proj)
        assert_eq(0, r.returncode, f"wrapper lock --sync failed: {r.stderr}")
        assert_eq(1, reg.count("/downloads/npm-cli-1.0.0.tgz"), "npm-cli .tgz downloaded once")
        assert_eq([], os.listdir(os.path.join(wrap_kugi_home, "tmp")), "tmp clean after wrapper sync")

        npm_sha = hashlib.sha256(npm_tgz).hexdigest()
        sdk_a_sha = hashlib.sha256(sdk_a_tgz).hexdigest()
        sdk_b_sha = hashlib.sha256(sdk_b_tgz).hexdigest()
        npm_store = os.path.join(wrap_kugi_home, "store", asset_store_key(npm_sha, unwrap=True))
        sdk_a_store = os.path.join(wrap_kugi_home, "store", asset_store_key(sdk_a_sha, unwrap=True))
        sdk_b_store = os.path.join(wrap_kugi_home, "store", asset_store_key(sdk_b_sha, unwrap=True))

        r = run_kugi(kugi_bin, ["status"], wrap_env, cwd=wrap_proj)
        assert_eq(0, r.returncode, f"wrapper status failed: {r.stderr}")
        assert_true("wrapper:npm-cli ok " in r.stdout, r.stdout)
        assert_true("wrapper:jsrt ok " in r.stdout, r.stdout)

        exec_env = dict(wrap_env)
        exec_env["NODE_PATH"] = "/existing/node"
        exec_env["PKG_CONFIG_PATH"] = "/existing/pc"
        r = run_kugi(kugi_bin, ["exec", "--", "npm-cli", "hello", "world"], exec_env, cwd=wrap_proj)
        assert_eq(0, r.returncode, f"exec npm-cli wrapper failed: {r.stderr}")
        assert_eq(
            f"npm-tgz-ok|production|{npm_store}/node_modules:/existing/node|no|hello|world",
            r.stdout,
            "npm-cli wrapper output mismatch",
        )

        # Wrapper with same name as tool overrides tool cleanly without recursion
        r = run_kugi(
            kugi_bin,
            ["exec", "--", "jsrt", "-c", 'printf "%s" "$WRAPPED_SELF"'],
            exec_env,
            cwd=wrap_proj,
        )
        assert_eq(0, r.returncode, f"same-name wrapper exec failed: {r.stderr}")
        assert_eq("yes", r.stdout, "same-name wrapper should take precedence over tool")

        # [env_prepend] preserves existing values and avoids trailing ':' when unset
        r = run_kugi(
            kugi_bin,
            ["exec", "--", "sh", "-c", 'printf "%s|%s" "$PKG_CONFIG_PATH" "$CPATH"'],
            exec_env,
            cwd=wrap_proj,
        )
        assert_eq(0, r.returncode, f"env_prepend exec failed: {r.stderr}")
        assert_eq(
            f"{sdk_a_store}/lib/pkgconfig:{sdk_b_store}/lib/pkgconfig:/existing/pc|{sdk_a_store}/include",
            r.stdout,
            "env_prepend exec mismatch",
        )

        # kugi env --shell sh & fish work with wrappers and [env_prepend]
        r = run_kugi(kugi_bin, ["env", "--shell", "sh"], exec_env, cwd=wrap_proj)
        assert_eq(0, r.returncode, f"wrapper env sh failed: {r.stderr}")
        eval_res = subprocess.run(
            [
                "sh",
                "-c",
                r.stdout + '\nprintf "%s||%s||%s" "$(npm-cli a b)" "$PKG_CONFIG_PATH" "$CPATH"',
            ],
            env=exec_env,
            capture_output=True,
            text=True,
        )
        assert_eq(0, eval_res.returncode, eval_res.stderr)
        assert_eq(
            f"npm-tgz-ok|production|{npm_store}/node_modules:/existing/node|no|a|b||{sdk_a_store}/lib/pkgconfig:{sdk_b_store}/lib/pkgconfig:/existing/pc||{sdk_a_store}/include",
            eval_res.stdout,
        )

        r = run_kugi(kugi_bin, ["env", "--shell", "fish"], exec_env, cwd=wrap_proj)
        assert_eq(0, r.returncode, f"wrapper env fish failed: {r.stderr}")
        assert_true(f"if set -q PKG_CONFIG_PATH; and test -n \"$PKG_CONFIG_PATH\"; set -gx PKG_CONFIG_PATH '{sdk_a_store}/lib/pkgconfig:{sdk_b_store}/lib/pkgconfig:'\"$PKG_CONFIG_PATH\"" in r.stdout, r.stdout)

        # Global manifest creates executable KUGI_HOME/bin/<wrapper> symlink
        wrap_global_dir = os.path.join(wrap_xdg, "kugi")
        os.makedirs(wrap_global_dir)
        jsrt_sha = hashlib.sha256(jsrt_tar).hexdigest()
        with open(os.path.join(wrap_global_dir, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                f'[tools.jsrt]\nversion = "1.0.0"\nurl = "{base_url}/downloads/jsrt-{{version}}.tar.gz"\n'
                f'bin = ["jsrt"]\nsha256 = "{jsrt_sha}"\n\n'
                f'[assets.npm_cli]\nversion = "1.0.0"\nurl = "{base_url}/downloads/npm-cli-{{version}}.tgz"\n'
                f'sha256 = "{npm_sha}"\n\n'
                f'[wrappers.glob-cli]\n'
                f'exec = "{{tool.jsrt}}/bin/jsrt"\n'
                f'args = ["{{asset.npm_cli}}/dist/cli.sh"]\n'
                f'env = {{ NODE_ENV = "global" }}\n'
            )
        wrap_empty = os.path.join(wrap_root, "empty")
        os.makedirs(wrap_empty)
        r = run_kugi(kugi_bin, ["sync"], wrap_env, cwd=wrap_empty)
        assert_eq(0, r.returncode, f"global wrapper sync failed: {r.stderr}")
        glob_wrap_link = os.path.join(wrap_kugi_home, "bin", "glob-cli")
        assert_true(os.path.islink(glob_wrap_link), "global wrapper symlink missing")
        direct_run = subprocess.run([glob_wrap_link, "x", "y"], env=wrap_env, capture_output=True, text=True)
        assert_eq(0, direct_run.returncode, direct_run.stderr)
        assert_eq("npm-tgz-ok|global||no|x|y", direct_run.stdout)

        # Cross-project deduplication across different tool/asset aliases + extract=bin vs extract=all isolation
        dedup_p1 = os.path.join(wrap_root, "dedup_p1")
        dedup_p2 = os.path.join(wrap_root, "dedup_p2")
        dedup_p3 = os.path.join(wrap_root, "dedup_p3")
        os.makedirs(dedup_p1)
        os.makedirs(dedup_p2)
        os.makedirs(dedup_p3)
        full_sha = hashlib.sha256(full_tar).hexdigest()
        with open(os.path.join(dedup_p1, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                f'[tools.alias_one]\nversion = "2.0.0"\nurl = "{base_url}/downloads/ztool-{{version}}.tar.gz"\n'
                f'extract = "bin"\nbin = ["ztool"]\nsha256 = "{full_sha}"\n\n'
                f'[assets.pack_one]\nversion = "1.0.0"\nurl = "{base_url}/downloads/npm-cli-{{version}}.tgz"\n'
                f'sha256 = "{npm_sha}"\n'
            )
        with open(os.path.join(dedup_p2, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                f'[tools.alias_two]\nversion = "2.0.0"\nurl = "{base_url}/downloads/ztool-{{version}}.tar.gz"\n'
                f'extract = "bin"\nbin = ["ztool"]\nsha256 = "{full_sha}"\n\n'
                f'[assets.pack_two]\nversion = "1.0.0"\nurl = "{base_url}/downloads/npm-cli-{{version}}.tgz"\n'
                f'sha256 = "{npm_sha}"\n'
            )
        with open(os.path.join(dedup_p3, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write(
                f'[tools.alias_one]\nversion = "2.0.0"\nurl = "{base_url}/downloads/ztool-{{version}}.tar.gz"\n'
                f'extract = "all"\nbin = ["ztool"]\nsha256 = "{full_sha}"\n'
            )

        reg.reset_counts()
        r = run_kugi(kugi_bin, ["sync"], wrap_env, cwd=dedup_p1)
        assert_eq(0, r.returncode, f"dedup_p1 sync failed: {r.stderr}")
        assert_eq(1, reg.count("/downloads/ztool-2.0.0.tar.gz"), "p1 downloads ztool once")
        assert_eq(0, reg.count("/downloads/npm-cli-1.0.0.tgz"), "p1 reuses npm-cli from earlier project")

        reg.reset_counts()
        r = run_kugi(kugi_bin, ["sync"], wrap_env, cwd=dedup_p2)
        assert_eq(0, r.returncode, f"dedup_p2 sync failed: {r.stderr}")
        assert_eq(0, reg.count("/downloads/ztool-2.0.0.tar.gz"), "p2 with different alias reuses same store entry")
        assert_eq(0, reg.count("/downloads/npm-cli-1.0.0.tgz"), "p2 with different asset alias reuses same store entry")

        reg.reset_counts()
        r = run_kugi(kugi_bin, ["sync"], wrap_env, cwd=dedup_p3)
        assert_eq(0, r.returncode, f"dedup_p3 sync failed: {r.stderr}")
        assert_eq(1, reg.count("/downloads/ztool-2.0.0.tar.gz"), "p3 extract=all uses distinct store entry from extract=bin")
        r = run_kugi(kugi_bin, ["exec", "--", "ztool"], wrap_env, cwd=dedup_p3)
        assert_eq(0, r.returncode, f"p3 exec ztool failed: {r.stderr}")
        assert_eq("hello-from-lib", r.stdout.strip(), "extract=all entry must retain lib/greeting.txt")

        # Validation failures: unknown ref in wrapper and [env]/[env_prepend] overlap
        bad_wrap_dir = os.path.join(wrap_root, "bad_wrap")
        os.makedirs(bad_wrap_dir)
        with open(os.path.join(bad_wrap_dir, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write('[wrappers.broken]\nexec = "{tool.nope}/bin/nope"\n')
        r = run_kugi(kugi_bin, ["sync"], wrap_env, cwd=bad_wrap_dir)
        assert_eq(2, r.returncode, "wrapper referencing unknown tool must exit 2")

        with open(os.path.join(bad_wrap_dir, "kugi.toml"), "w", encoding="utf-8") as f:
            f.write('[env]\nFOO = "1"\n\n[env_prepend]\nFOO = "2"\n')
        r = run_kugi(kugi_bin, ["sync"], wrap_env, cwd=bad_wrap_dir)
        assert_eq(2, r.returncode, "overlapping [env] and [env_prepend] must exit 2")

        # Dead-worktree GC reclaims project wrappers and unique assets while keeping global wrapper & shared assets alive
        shutil.rmtree(wrap_proj)
        r = run_kugi(kugi_bin, ["gc"], wrap_env, cwd=wrap_root)
        assert_eq(0, r.returncode, f"wrapper gc failed: {r.stderr}")
        assert_true("deleted_roots: 1" in r.stdout, r.stdout)
        assert_true("deleted_store_entries: 4" in r.stdout, r.stdout)
        assert_true(not os.path.exists(sdk_a_store), "dead sdk_a must be reclaimed")
        assert_true(not os.path.exists(sdk_b_store), "dead sdk_b must be reclaimed")
        assert_true(os.path.isdir(npm_store), "shared npm_cli used by global wrapper must survive GC")
        direct_run2 = subprocess.run([glob_wrap_link, "after", "gc"], env=wrap_env, capture_output=True, text=True)
        assert_eq(0, direct_run2.returncode, direct_run2.stderr)
        assert_eq("npm-tgz-ok|global||no|after|gc", direct_run2.stdout)

    srv.shutdown()
    elapsed = time.monotonic() - t0
    print(f"integration: ok ({elapsed:.2f}s)")


if __name__ == "__main__":
    main()
