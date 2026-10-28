"""DBFerry - sunucular arasi PostgreSQL / Docker veritabani tasima araci.

Yetenekler:
  * Yerel dokum dosyasini uzak sunucudaki Docker PostgreSQL'e geri yukler
  * Yerel dokum dosyasini bu bilgisayardaki PostgreSQL'e geri yukler
  * Bir sunucudan diger sunucuya canli veritabani kopyalar
  * Uzak sunucudan yerel diske yedek alir
  * Bu bilgisayardaki PostgreSQL veritabanini yerel diske yedekler

Tum islemler plan -> onay -> uygulama -> dogrulama akisiyla yurutulur ve
adim adim canli olarak loglanir.
"""

__version__ = "1.0.0"

APP_NAME = "DBFerry"
APP_TAGLINE = "Veritabanlarini sunucular arasinda tasi"
