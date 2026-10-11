FROM python:3.11-slim
RUN pip install --no-cache-dir aiohttp==3.13.5
WORKDIR /app
COPY tap.py test_tap.py ./
EXPOSE 9090 9091
CMD ["python", "-u", "tap.py"]
