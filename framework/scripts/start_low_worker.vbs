Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "D:\PersonalKB"
' 低负载工人：OCR(CPU) + 嵌入(NPU)；--ocr-device 可换 gpu/npu
sh.Run """pythonw -m kb low --instance 3 --cpu-start 9 --ocr-device cpu --embed 8 --no-audio --ocr 1 --cores 2 --no-busy""", 0, False
