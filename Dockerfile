FROM python:3.12-alpine

WORKDIR /app

RUN adduser -D -u 10001 appuser
COPY app.py /app/app.py

USER appuser
EXPOSE 8080

CMD ["python3", "/app/app.py"]
