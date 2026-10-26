import subprocess, sys
subprocess.Popen([sys.executable, "-m", "kb", "api"],
                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
