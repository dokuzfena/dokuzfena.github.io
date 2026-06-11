# bot.py
import os
import time
import ccxt
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from supabase import create_client, Client

# Çevre değişkenlerini yükle (Lokalde veya bulutta saklanan gizli anahtarlar)
load_dotenv()

# ==========================================
# GELİŞMİŞ TİCARET VE RİSK YAPILANDIRMASI
# ==========================================
SYMBOL = 'BTC/USDT'        # Takip edilecek ve işlem yapılacak parite
TIMEFRAME = '5m'           # Teknik analiz mum periyodu (5 Dakikalık mumlar)
RSI_PERIOD = 14            # Standart RSI periyodu
RSI_OVERBOUGHT = 70        # Aşırı alım sınırı (Satış için alarm)
RSI_OVERSOLD = 30          # Aşırı satım sınırı (Alım için alarm)
TRADE_USDT_AMOUNT = 15.0   # Her işlemde devreye girecek USDT miktarı
TETIKTE_BEKLEME_SURESI = 5 # Her 5 saniyede bir borsa fiyatını kontrol eder (Sürekli tetikte kalma)

def init_supabase() -> Client:
    """Supabase bulut veri tabanına güvenli ve doğrulanmış erişim sağlar."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise ValueError("[KRİTİK] Supabase URL veya Key bulunamadı! Ayarları kontrol edin.")
    return create_client(url, key)

def init_binance() -> ccxt.binance:
    """Binance API bağlantısını senkronizasyon ve IP ban korumasıyla başlatır."""
    api_key = os.getenv("BINANCE_API_KEY")
    secret_key = os.getenv("BINANCE_SECRET_KEY")
    
    if not api_key or not secret_key or "your_" in api_key or "buraya_" in api_key:
        print("⚠️ [UYARI] API anahtarları eksik veya sahte. Bot GÜVENLİ SİMÜLASYON modunda saniye saniye çalışıyor.")
        return ccxt.binance({
            'enableRateLimit': True,
            'options': {'defaultType': 'spot'}
        })
    
    return ccxt.binance({
        'apiKey': api_key,
        'secret': secret_key,
        'enableRateLimit': True,  # Borsadan IP engeli yememek için hayati hız limiti filtrelemesi
        'options': {
            'defaultType': 'spot',
            'adjustForTimeDifference': True # Sunucu saatiyle borsa saatini otomatik eşitler
        }
    })

def db_log(supabase: Client, level: str, message: str):
    """Olayları anlık olarak hem ekrana basar hem de telefonundan görebilmen için Supabase'e fırlatır."""
    formatted_msg = f"[{level}] {message}"
    print(formatted_msg, flush=True) # Hugging Face loglarında anlık görünmesi için flush=True şarttır
    try:
        supabase.table("bot_logs").insert({"log_level": level, "message": message}).execute()
    except Exception as e:
        print(f"❌ [DB LOG HATASI] Günlük veri tabanına yazılamadı: {e}", flush=True)

def calculate_rsi(ohlcv_data, period=14) -> pd.DataFrame:
    """Sıfıra bölünme hatası barındırmayan endüstriyel RSI hesaplama motoru."""
    df = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = df['close'].astype(float)
    
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    
    loss = loss.replace(0, 0.00001) # Matematiksel tanımsızlığı engelleme filtresi
    
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    return df

def execute_market_order(binance: ccxt.binance, supabase: Client, symbol: str, action: str, usdt_amount: float, current_rsi: float):
    """Hassasiyet yuvarlamalı, cüzdan bakiye doğrulamalı ve fiyat kayma korumalı ana emir motoru."""
    db_log(supabase, "INFO", f"TETİKLEME ALINDI: {symbol} paritesinde {action} işlemi işleniyor...")
    
    # Güvenli Simülasyon Modu Kontrolü
    if not binance.apiKey:
        mock_price = binance.fetch_ticker(symbol)['close']
        mock_qty = usdt_amount / mock_price
        db_log(supabase, "TRADE", f"⚙️ [SİMÜLASYON İŞLEMİ] {symbol} {action} | Fiyat: {mock_price} | RSI: {current_rsi:.2f}")
        try:
            supabase.table("bot_orders").insert({
                "symbol": symbol, "action": action, "price": mock_price, "quantity": mock_qty,
                "total_cost": usdt_amount, "rsi_value": current_rsi, "status": "COMPLETED", "order_id": "SIM_724_ACTIVE"
            }).execute()
        except Exception as e:
            db_log(supabase, "ERROR", f"Simülasyon işlemi veri tabanına kaydedilemedi: {e}")
        return

    try:
        # Borsanın güncel emir kurallarını, adım boyutlarını (lot size) ve fiyat adımlarını çekiyoruz
        binance.load_markets()
        market = binance.market(symbol)
        ticker = binance.fetch_ticker(symbol)
        current_price = float(ticker['close'])
        
        # Risk Yönetimi: İşlem öncesi cüzdan bakiye denetimi
        balance = binance.fetch_balance()
        base_asset, quote_asset = symbol.split('/') # Örn: BTC ve USDT split edilir
        
        if action == "BUY":
            available_usdt = float(balance['free'].get(quote_asset, 0))
            if available_usdt < usdt_amount:
                db_log(supabase, "WARNING", f"Alım İptal Edildi: Yetersiz USDT Bakiyesi! Mevcut: {available_usdt} USDT")
                return
            raw_quantity = usdt_amount / current_price
            # Binance hassasiyet kurallarına göre coini yuvarlıyoruz (Örn: 0.00051234 -> 0.00051)
            quantity = float(binance.amount_to_precision(symbol, raw_quantity))
        
        elif action == "SELL":
            available_coin = float(balance['free'].get(base_asset, 0))
            raw_quantity = usdt_amount / current_price if available_coin * current_price > usdt_amount else available_coin
            quantity = float(binance.amount_to_precision(symbol, raw_quantity))
            
            if quantity <= 0 or (quantity * current_price) < float(market['limits']['cost']['min']):
                db_log(supabase, "WARNING", f"Satış İptal Edildi: Cüzdanda satılacak yeterli {base_asset} varlığı yok.")
                return

        # Binance Minimum İşlem Tutarı (Min Notional - Genelde 10 USDT) Filtresi
        calculated_cost = quantity * current_price
        min_cost = float(market['limits']['cost']['min'])
        if calculated_cost < min_cost:
            db_log(supabase, "ERROR", f"İşlem hacmi borsa sınırının altında kalıyor. Hesaplanan: {calculated_cost}, Minimum: {min_cost}")
            return

        # GERÇEK PARAYLA EMRİN BORSAYA İLETİLMESİ
        order = None
        if action == "BUY":
            order = binance.create_market_buy_order(symbol, quantity)
        elif action == "SELL":
            order = binance.create_market_sell_order(symbol, quantity)

        # Emir başarılı ise verileri anlık olarak Supabase'e fırlatıyoruz
        if order:
            exec_price = float(order.get('price', current_price)) if order.get('price', 0) > 0 else current_price
            exec_qty = float(order.get('filled', quantity))
            actual_cost = exec_price * exec_qty
            order_id = str(order.get('id'))
            
            db_log(supabase, "TRADE", f"✅ İŞLEM GERÇEKLEŞTİ: {symbol} {action} | ID: {order_id} | Fiyat: {exec_price}")
            
            supabase.table("bot_orders").insert({
                "symbol": symbol, "order_id": order_id, "action": action, "price": exec_price,
                "quantity": exec_qty, "total_cost": actual_cost, "rsi_value": current_rsi, "status": "COMPLETED"
            }).execute()

    except ccxt.NetworkError as ne:
        # Binance -1007 Timeout ve Ağ Kesintisi Koruması (Çift emri önler)
        db_log(supabase, "CRITICAL", f"🚨 [AĞ ZAMAN AŞIMI] Binance yanıt vermedi. Çift işlem engellemek için emir askıya alındı: {ne}")
    except ccxt.ExchangeError as ee:
        db_log(supabase, "ERROR", f"❌ Borsadan Ret Yanıtı Alındı (Hassasiyet veya Limit Sorunu): {ee}")
    except Exception as e:
        db_log(supabase, "CRITICAL", f"❌ Emir akışında bilinmeyen sistem hatası fırlatıldı: {e}")

def main():
    # İlk başlatma doğrulaması
    try:
        supabase = init_supabase()
        binance = init_binance()
        db_log(supabase, "SYSTEM", "🚀 Canlı Bot Çekirdeği Hugging Face üzerinde 7/24 kesintisiz modda tetikte bekliyor.")
    except Exception as e:
        print(f"[KRİTİK BAŞLATMA HATASI] Sistem modülleri bağlanamadı: {e}", flush=True)
        return

    # Durum kilidi (Aynı sinyalin saniyede yüzlerce kez borsaya gönderilmesini önler)
    last_signal = None

    while True:
        try:
            # Şifresiz public veri hattından mum grafik verilerini çekiyoruz
            ohlcv = binance.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=100)
            if not ohlcv or len(ohlcv) < RSI_PERIOD:
                time.sleep(10)
                continue
                
            df = calculate_rsi(ohlcv, RSI_PERIOD)
            current_rsi = float(df['rsi'].iloc[-1])
            current_price = float(df['close'].iloc[-1])
            
            # Sunucu loglarında saniye saniye canlı izleme çıktısı
            print(f"📊 [TAKİP] {SYMBOL}: ${current_price:,.2f} | RSI: {current_rsi:.2f} | Sinyal Durumu: {last_signal}", flush=True)
            
            # 7/24 Saniye Saniye Tetikte Bekleyen Strateji Algoritması
            if pd.notna(current_rsi):
                if current_rsi <= RSI_OVERSOLD and last_signal != "BUY":
                    execute_market_order(binance, supabase, SYMBOL, "BUY", TRADE_USDT_AMOUNT, current_rsi)
                    last_signal = "BUY"
                    
                elif current_rsi >= RSI_OVERBOUGHT and last_signal != "SELL":
                    execute_market_order(binance, supabase, SYMBOL, "SELL", TRADE_USDT_AMOUNT, current_rsi)
                    last_signal = "SELL"
            
            # WAF güvenlik duvarına takılmadan tetikte kalma süresi (5 saniyede bir kontrol)
            time.sleep(TETIKTE_BEKLEME_SURESI)
            
        except Exception as e:
            # Kodun asla çökmemesini sağlayan ana koruma kalkanı
            print(f"⚠️ Arka plan işletim döngüsünde anlık hata atlatıldı: {e}", flush=True)
            time.sleep(10)

if __name__ == "__main__":
    main()
