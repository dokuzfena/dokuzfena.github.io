# Dockerfile
# Güvenilir ve optimize edilmiş resmi Python imajını kullanıyoruz
FROM python:3.10-slim

# Sunucu içinde çalışma klasörü oluşturuyoruz
WORKDIR /app

# Sistem bağımlılıklarını ve güvenlik güncellemelerini kuruyoruz
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Gerekli kütüphane listesini sunucuya kopyalıyoruz
COPY requirements.txt .

# Kütüphaneleri terminale gerek kalmadan otomatik yüklüyoruz
RUN pip install --no-cache-dir -r requirements.txt

# Bot kodumuzu sunucuya kopyalıyoruz
COPY bot.py .

# Çevre değişkenlerinin canlı akışını doğrulamak için Python'ı unbuffered moda alıyoruz
ENV PYTHONUNBUFFERED=1

# Botu 7/24 kesintisiz döngüde başlatacak ana tetikleyici komut
CMD ["python", "bot.py"]
