FROM python:3.9-slim

WORKDIR /app
COPY alarm.py .

EXPOSE 8484

CMD ["python", "-u", "alarm.py"]
