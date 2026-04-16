FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir fastapi uvicorn numpy
EXPOSE 8001
CMD ["python", "main.py"]
