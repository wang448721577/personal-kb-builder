Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "D:\PersonalKB"
' 轻量守护：文件监听 + 屏幕记忆 + 微信 + 嵌入；改路径后运行 cscript start_daemon.vbs
sh.Run """pythonw -m kb daemon --no-ocr --no-transcribe --no-embed""", 0, False
