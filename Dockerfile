FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DBFERRY_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY dbf.py ./
COPY dbf_ferry/ ./dbf_ferry/

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8787 8765

# Varsayilan: MCP sunucusu (stdio). Web konsolu icin:
#   docker run -p 8787:8787 -v dbferry-data:/data dbferry web --host 0.0.0.0 --no-browser
ENTRYPOINT ["python", "dbf.py"]
CMD ["mcp"]
