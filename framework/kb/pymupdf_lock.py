import threading

# PyMuPDF/MuPDF is not thread-safe; all calls in this process must serialize.
PDF_LOCK = threading.RLock()
