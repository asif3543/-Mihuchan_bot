# Lightweight image — sirf frontend bot (app.py) yahan chalta hai.
# Heavy OCR/translator stack (torch, manga-image-translator) is image me
# NAHI hai — wo kaam GitHub Actions runner (manga.yml) par hota hai.
FROM python:3.11-slim

WORKDIR /app

# System deps (agar future me pillow/psutil ko native build chahiye ho)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Render $PORT env var runtime par set karta hai; app.py isko khud read karta hai.
# Yeh EXPOSE sirf documentation ke liye hai, Render isse ignore karke apna
# $PORT use karta hai.
EXPOSE 7860

CMD ["python", "app.py"]
