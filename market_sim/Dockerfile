FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    "yfinance>=0.2.40" \
    "pandas>=2.0"
EXPOSE 8001
CMD ["python", "main.py"]
