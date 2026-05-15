FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml /app/
RUN pip install --no-cache-dir -e .

COPY app.py /app/

ENTRYPOINT ["python", "app.py"]
