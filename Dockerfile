FROM python:3.9-slim

WORKDIR /app
COPY alarm.py .

EXPOSE 8080

CMD ["python", "-u", "alarm.py"]
