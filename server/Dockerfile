FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./requirements.txt
COPY app.py ./app.py
RUN pip install --no-cache-dir -r requirements.txt


EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
