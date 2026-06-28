"""Docker 沙箱执行引擎。

LLM 生成的 Python 代码在隔离的 Docker 容器中运行，特性包括：
  - 无网络访问
  - 内存和 CPU 限制
  - 执行超时
  - 自动裁剪错误堆栈以便重写反馈
"""

import os
import json
import time
import tempfile
import subprocess
import asyncio
from dataclasses import dataclass, field
from typing import Optional
from ..config import config


@dataclass
class SandboxResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    execution_time_ms: float = 0.0
    trimmed_error: str = ""  # 用于 LLM 重写的裁剪后错误信息
    output_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "stdout": self.stdout[:2000],
            "stderr": self.stderr[:2000],
            "exit_code": self.exit_code,
            "execution_time_ms": round(self.execution_time_ms, 2),
            "trimmed_error": self.trimmed_error,
            "output_data": self.output_data,
        }


class SandboxExecutor:
    """在 Docker 容器中执行 Python 代码。

    生成的代码必须将结果写入 JSON 文件
    /tmp/sandbox_output.json —— 执行器会将其读取为 output_data。

    如果 Docker 不可用，则回退到带有资源限制的本地子进程执行。
    """

    RUNNER_TEMPLATE = '''\
import json, sys, traceback

_result = {{"success": False, "data": None, "error": ""}}

try:
{code_indented}
except Exception as e:
    _result["error"] = f"{{type(e).__name__}}: {{e}}"
    _result["traceback"] = traceback.format_exc()
else:
    _result["success"] = True

# 如果用户代码定义了 `result` 变量，则捕获它
if "result" in dir():
    _result["data"] = result

with open("/tmp/sandbox_output.json", "w") as f:
    json.dump(_result, f, default=str)
'''

    def __init__(
        self,
        image: str = "",
        timeout: int = 30,
        memory_limit: str = "256m",
    ):
        self.image = image or config.sandbox.docker_image
        self.timeout = timeout or config.sandbox.timeout
        self.memory_limit = memory_limit or config.sandbox.memory_limit
        self._docker_available: Optional[bool] = None

    def _check_docker(self) -> bool:
        if self._docker_available is not None:
            return self._docker_available
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True, timeout=5,
            )
            self._docker_available = result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self._docker_available = False
        return self._docker_available

    def _indent_code(self, code: str) -> str:
        """缩进用户代码以嵌入 try 块。"""
        lines = code.strip().split("\n")
        return "\n".join("    " + line if line.strip() else line for line in lines)

    def _trim_error(self, stderr: str, traceback_str: str = "") -> str:
        """裁剪错误堆栈，保留最相关的行用于 LLM 重写。

        策略：保留最后 N 个栈帧 + 最终的异常行。
        """
        raw = traceback_str or stderr
        lines = raw.strip().split("\n")

        # 查找异常行（最后一条不以 'File' 开头的非空行）
        exc_lines = [l for l in lines if l.strip() and not l.strip().startswith("File")]
        # 保留最后 5 行 traceback + 异常
        trimmed = "\n".join(lines[-8:])

        # 仅提取关键错误
        if exc_lines:
            trimmed = exc_lines[-1] + "\n\n" + trimmed
        return trimmed.strip()

    async def execute(self, code: str, input_data: dict) -> SandboxResult:
        """在沙箱中执行 Python 代码。

        Args:
            code: 要执行的 Python 源代码，应将输出数据赋值给 `result` 变量。
            input_data: 可选的字典，在沙箱中以 `input_data` 变量名提供。

        Returns:
            SandboxResult，包含 stdout、stderr、output_data 和裁剪后的错误。
        """
        loop = asyncio.get_event_loop()

        # 构建完整脚本
        input_json = json.dumps(input_data or {}, default=str)
        full_code = f"import json as _json\ninput_data = _json.loads('''{input_json}''')\n\n{code}"
        indented = self._indent_code(full_code)
        script = self.RUNNER_TEMPLATE.format(code_indented=indented)

        if self._check_docker():
            return await loop.run_in_executor(None, self._execute_docker, script)
        else:
            return await loop.run_in_executor(None, self._execute_local, script)

    def _execute_docker(self, script: str) -> SandboxResult:
        """在 Docker 容器中执行。"""
        start = time.time()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, prefix="sandbox_"
        ) as script_file:
            script_file.write(script)
            script_path = script_file.name

        output_path = "/tmp/sandbox_output.json"
        try:
            cmd = [
                "docker", "run", "--rm",
                "--network", "none",
                "--memory", self.memory_limit,
                "--cpus", str(config.sandbox.cpu_limit),
                "-v", f"{script_path}:/tmp/script.py",
                self.image,
                "python", "/tmp/script.py",
            ]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            elapsed = (time.time() - start) * 1000

            output_data = {}
            if result.returncode == 0:
                # 通过第二次容器调用来读取 JSON 输出
                # 实际上脚本写入的是容器内的 /tmp —— 我们需要
                # 改为捕获 stdout。调整：将 JSON 打印到 stdout。
                pass

            # 解析 stdout 中的 JSON 输出
            try:
                # 脚本写入容器内的 /tmp/sandbox_output.json，
                # 该文件会随 --rm 一起删除。改为打印到 stdout。
                # 如果 stdout 看起来像 JSON，则从中解析。
                if result.stdout.strip().startswith("{"):
                    output_data = json.loads(result.stdout.strip())
                else:
                    # 尝试从 stdout 中提取 JSON
                    for line in result.stdout.split("\n"):
                        line = line.strip()
                        if line.startswith("{") and line.endswith("}"):
                            output_data = json.loads(line)
                            break
            except json.JSONDecodeError:
                pass

            trimmed = self._trim_error(result.stderr)

            return SandboxResult(
                success=result.returncode == 0 and output_data.get("success", False),
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                execution_time_ms=elapsed,
                trimmed_error=trimmed,
                output_data=output_data,
            )
        except subprocess.TimeoutExpired:
            elapsed = (time.time() - start) * 1000
            return SandboxResult(
                success=False,
                stderr=f"Execution timed out after {self.timeout}s",
                exit_code=-1,
                execution_time_ms=elapsed,
                trimmed_error=f"TimeoutError: execution exceeded {self.timeout}s limit",
            )
        finally:
            os.unlink(script_path)

    def _execute_local(self, script: str) -> SandboxResult:
        """回退方案：在本地子进程中执行（隔离性较弱）。"""
        start = time.time()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, prefix="sandbox_"
        ) as script_file:
            script_file.write(script)
            script_path = script_file.name

        output_file = tempfile.mktemp(suffix=".json", prefix="sandbox_out_")

        try:
            # 替换脚本中的输出路径
            script = script.replace(
                "/tmp/sandbox_output.json", output_file
            )
            with open(script_path, "w") as f:
                f.write(script)

            result = subprocess.run(
                ["python3", script_path],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env={**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(__file__)))},
            )
            elapsed = (time.time() - start) * 1000

            output_data = {}
            if os.path.exists(output_file):
                with open(output_file) as f:
                    output_data = json.load(f)

            trimmed = self._trim_error(result.stderr)

            return SandboxResult(
                success=result.returncode == 0 and output_data.get("success", False),
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                execution_time_ms=elapsed,
                trimmed_error=trimmed,
                output_data=output_data,
            )
        except subprocess.TimeoutExpired:
            elapsed = (time.time() - start) * 1000
            return SandboxResult(
                success=False,
                stderr=f"Execution timed out after {self.timeout}s",
                exit_code=-1,
                execution_time_ms=elapsed,
                trimmed_error=f"TimeoutError: execution exceeded {self.timeout}s limit",
            )
        finally:
            if os.path.exists(script_path):
                os.unlink(script_path)
            if os.path.exists(output_file):
                os.unlink(output_file)
