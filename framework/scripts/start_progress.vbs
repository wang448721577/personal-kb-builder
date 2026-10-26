Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "D:\PersonalKB"
sh.Run """pythonw -m kb progress""", 0, False
