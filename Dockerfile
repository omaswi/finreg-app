FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
WORKDIR /app
COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create a directory for static frontend files
WORKDIR /var/www/html

# Copy the frontend files into it
COPY frontend/ .

# Switch back to the app directory for the main command
WORKDIR /app

# Create a directory for file uploads
RUN mkdir -p /app/uploads
VOLUME /app/uploads

EXPOSE 5000
CMD ["python", "-m", "gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
