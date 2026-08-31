import subprocess
import sys

subprocess.run(
    [sys.executable, "-m", "uvicorn", "main:app", "--reload", "--host", "0.0.0.0"],
    cwd=__file__.replace("run.py", "")
)

