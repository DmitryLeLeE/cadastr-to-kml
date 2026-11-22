# Кадастр НСПД и Координаты ➔ KML / KMZ

Инструмент для конвертации координат и выгрузки границ объектов из публичного API НСПД в форматы **KML** и **KMZ** (с автоматическим пересчётом из EPSG:3857 в WGS84).

🌐 **Онлайн-версия (без установки):** [https://dmitrylelee.github.io/cadastr-to-kml/](https://dmitrylelee.github.io/cadastr-to-kml/)

## Возможности
- **Онлайн в браузере (GitHub Pages):**
  - Ввод координат списком (`Lat, Lon`, `Lon, Lat` или метры `EPSG:3857`) → Полигон / Линия / Точки.
  - Разбор JSON/GeoJSON из НСПД → автоматическая конвертация в WGS84.
  - Предпросмотр на интерактивной карте и скачивание `.kml` / `.kmz`.
- **Десктопное приложение (`cadastr_to_kml.py`):** выгрузка по кадастровому номеру напрямую из НСПД.
- **Веб-сервер (`web_app.py`):** FastAPI-бэкенд для прямого онлайн-поиска по кадастровому номеру.

## Быстрый старт (локально)

```bash
git clone https://github.com/DmitryLeLeE/cadastr-to-kml.git
cd cadastr-to-kml
pip install -r requirements.txt