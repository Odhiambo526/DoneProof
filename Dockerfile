FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY doneproof ./doneproof
RUN pip install --no-cache-dir .
ENV DONEPROOF_DB=/data/doneproof.db
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "doneproof.app:app", "--host", "0.0.0.0", "--port", "8000"]
