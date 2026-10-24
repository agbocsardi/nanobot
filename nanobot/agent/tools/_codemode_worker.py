"""Standalone stdlib-only codemode worker. Run only inside the OS sandbox."""

import ast
import asyncio
import ctypes
import errno
import json
import math
import resource
import sys
import traceback

_PROTOCOL = sys.stdout
_PENDING = {}
_SEQUENCE = 0


def _send(message):
    _PROTOCOL.write(json.dumps(message, ensure_ascii=True, allow_nan=False) + "\n")
    _PROTOCOL.flush()


def text(value=""):
    """Emit only the data the model needs; other tool results stay in the worker."""
    rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
    _send({"type": "output", "text": rendered + "\n"})


class _Output:
    def write(self, value):
        if value:
            _send({"type": "output", "text": value})
        return len(value)

    def flush(self):
        pass


class ToolError(Exception):
    """An unsuccessful tool outcome; ``result`` retains its structured metadata."""

    def __init__(self, result):
        self.result = result
        super().__init__(result["content"])


async def _request(operation, **arguments):
    global _SEQUENCE
    _SEQUENCE += 1
    request_id = _SEQUENCE
    future = asyncio.get_running_loop().create_future()
    _PENDING[request_id] = future
    try:
        _send({"type": "request", "id": request_id, "operation": operation, **arguments})
        return await future
    finally:
        _PENDING.pop(request_id, None)


class _Tools:
    def __getitem__(self, name):
        async def call(**arguments):
            result = await _request("call", name=name, arguments=arguments)
            if result["status"] not in ("success", "nonzero_exit", "ask_user"):
                raise ToolError(result)
            return result
        return call

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]


async def search_tools(query="", *, namespace=None, limit=8, offset=0):
    return await _request("search", query=query, namespace=namespace, limit=limit, offset=offset)


async def describe_tool(name):
    return await _request("describe", name=name)


async def worker(task, *, artifacts=None, material=""):
    return await _request("worker", arguments={
        "task": task, "artifacts": artifacts, "material": material,
    })


async def artifact_put(text):
    return await _request("artifact_put", arguments={"text": text})


async def artifact_read(artifact, *, offset=0, limit=8000):
    return await _request("artifact_read", arguments={
        "artifact": artifact, "offset": offset, "limit": limit,
    })


async def _read_replies(reader):
    try:
        while line := await reader.readline():
            reply = json.loads(line)
            future = _PENDING.get(reply["id"])
            if future is not None and not future.done():
                if reply.get("ok"):
                    future.set_result(reply["result"])
                else:
                    future.set_exception(RuntimeError(reply["error"]))
    finally:
        for future in list(_PENDING.values()):
            if not future.done():
                future.set_exception(RuntimeError("Tool bridge disconnected"))


async def _main(payload):
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=4 * 1024 * 1024)
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    replies = asyncio.create_task(_read_replies(reader))
    sys.stdout = _Output()
    sys.stderr = _Output()
    namespace = {
        "__name__": "__codemode__", "asyncio": asyncio, "json": json,
        "tools": _Tools(), "text": text, "ToolError": ToolError,
        "search_tools": search_tools, "describe_tool": describe_tool,
        "worker": worker, "artifact_put": artifact_put, "artifact_read": artifact_read,
    }
    try:
        # Build the async wrapper without shifting the submitted source's line numbers.
        body = ast.parse(payload["code"] or "pass", filename="<codemode>").body
        wrapper = ast.parse("async def __script__():\n    pass", filename="<codemode>")
        if body:
            wrapper.body[0].body = body
            wrapper.body[0].end_lineno = body[-1].end_lineno
            wrapper.body[0].end_col_offset = body[-1].end_col_offset
        exec(compile(wrapper, "<codemode>", "exec"), namespace)
        result = await namespace["__script__"]()
        if result is not None:
            text(result)
    except BaseException:
        _send({"type": "finished", "ok": False, "error": traceback.format_exc(limit=6)})
    else:
        _send({"type": "finished", "ok": True})
    finally:
        replies.cancel()
        await asyncio.gather(replies, return_exceptions=True)
        transport.close()


def _drop_setup_capabilities():
    """Modern bwrap needs NET_ADMIN for lo; generated Python must retain none.

    SETPCAP is used only by this trusted bootstrap to empty the bounding set.
    Clearing bounding/permitted/effective/inheritable/ambient sets prevents
    regaining setup privileges through exec, including when running as uid 0.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    for cap in range(64):
        if libc.prctl(24, cap, 0, 0, 0) != 0:  # PR_CAPBSET_DROP
            error = ctypes.get_errno()
            if error != errno.EINVAL:  # Capabilities beyond this kernel's last cap.
                raise OSError(error, "Cannot drop sandbox setup capability bounding set")
    if libc.prctl(47, 4, 0, 0, 0) != 0:  # PR_CAP_AMBIENT / CLEAR_ALL
        raise OSError(ctypes.get_errno(), "Cannot clear sandbox ambient capabilities")
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "Cannot set sandbox no_new_privs")

    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class Data(ctypes.Structure):
        _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]

    header = Header(0x20080522, 0)  # _LINUX_CAPABILITY_VERSION_3
    data = (Data * 2)()
    libc.capset.argtypes = [ctypes.POINTER(Header), ctypes.POINTER(Data)]
    libc.capset.restype = ctypes.c_int
    if libc.capset(ctypes.byref(header), data) != 0:
        raise OSError(ctypes.get_errno(), "Cannot clear sandbox setup capabilities")


if __name__ == "__main__":
    _drop_setup_capabilities()
    # This first message is supplied only by the parent, before any generated code.
    config = json.loads(sys.stdin.buffer.readline())
    for kind, limit in (
        (resource.RLIMIT_AS, config["memory_mb"] * 1024 * 1024),
        (resource.RLIMIT_CPU, math.ceil(config["timeout_s"])),
        (resource.RLIMIT_FSIZE, 8 * 1024 * 1024),
        (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_CORE, 0),
    ):
        resource.setrlimit(kind, (limit, limit))
    asyncio.run(_main(config))
