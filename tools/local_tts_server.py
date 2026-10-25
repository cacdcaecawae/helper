"""本地 Qwen3-TTS 语音服务：只实现 helper 用到的 Fish Audio 接口子集，仅监听本机。

运行：.venv-tts\\Scripts\\python.exe tools\\local_tts_server.py（管理台 web.py 启动时会自动无窗口启动它，
带 --parent 管理台进程号：管理台退出时本服务跟着退出）。不要加 -s：torch 装在用户目录的 site-packages 里。
固定随机种子，同一音色、同一句话每次合成结果一致。
"""
import hashlib
import io
import json
import os
import sys
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MODEL_DIR = r"F:\本地模型\Qwen3-TTS-12Hz-1.7B-CustomVoice"
PORT = 8766
SEED = 0


def voice_id(name):
    return hashlib.md5(name.encode()).hexdigest()


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, content_type="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path != "/model":
            return self.send(404, {"message": "本地服务没有公开音色"})
        own = parse_qs(url.query).get("self") == ["true"]
        items = [{"_id": key, "title": name.title(), "type": "tts", "state": "trained", "visibility": "private"}
                 for key, name in VOICES.items()] if own else []
        self.send(200, {"items": items, "has_more": False})

    def do_POST(self):
        if urlsplit(self.path).path != "/v1/tts":
            return self.send(404, {"message": "not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 65536:
                raise ValueError
            body = json.loads(self.rfile.read(length))
            text, speaker = body["text"], VOICES[body["reference_id"]]
            if not isinstance(text, str) or not text.strip():
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return self.send(400, {"message": "请求格式不正确或音色不存在"})
        try:
            # ponytail: 全局锁串行合成，单卡够用；多卡/并发需求再做队列。
            with LOCK:
                torch.manual_seed(SEED)
                wavs, rate = MODEL.generate_custom_voice(text=text, language="Chinese", speaker=speaker)
        except Exception as error:  # 合成失败只回状态码，不回显正文
            print("合成失败：", type(error).__name__, flush=True)
            return self.send(500, {"message": "合成失败"})
        pcm = (np.clip(wavs[0], -1, 1) * 32767).astype("<i2").tobytes()
        output = io.BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(rate)
            target.writeframes(pcm)
        self.send(200, output.getvalue(), "audio/wav")

    def log_message(self, format, *args):
        pass  # 不记录请求，避免聊天内容落盘


def exit_with(pid):
    """管理台（进程号 pid）一退出（含直接关窗口）就跟着退出，不在后台占显存。等的是进程句柄、不碰标准输入：
    后台线程同步读 stdin 管道时，主线程加载 torch 等库要查询 stdin，会被一起卡住，模型就一直加载不完。"""
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:  # 管理台已经不在了
        os._exit(0)
    threading.Thread(target=lambda: (kernel32.WaitForSingleObject(handle, 0xFFFFFFFF), os._exit(0)), daemon=True).start()


if __name__ == "__main__":
    # 先占端口：已有实例在运行时直接退出，不重复加载模型。
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    if "--parent" in sys.argv:
        exit_with(int(sys.argv[sys.argv.index("--parent") + 1]))
    import numpy as np
    import torch
    from qwen_tts import Qwen3TTSModel

    MODEL = Qwen3TTSModel.from_pretrained(MODEL_DIR, dtype=torch.bfloat16,
                                          device_map="cuda:0" if torch.cuda.is_available() else "cpu")
    VOICES = {voice_id(name): name for name in MODEL.get_supported_speakers()}
    LOCK = threading.Lock()
    print(f"本地语音服务已就绪：http://127.0.0.1:{PORT}，音色：{', '.join(n.title() for n in VOICES.values())}", flush=True)
    server.serve_forever()
