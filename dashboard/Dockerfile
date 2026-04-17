FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY fund/       fund/
COPY dashboard/  dashboard/
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "dashboard.main:app", "--host", "0.0.0.0", "--port", "8080"]
