
FROM python:3.12-slim

# Set a working directory inside the container
WORKDIR /app

# Copy the current directory contents into the container
COPY . .

# Install required packages
RUN pip install --no-cache-dir -r requirements.txt

# Expose the port your Flask app runs on
EXPOSE 8080

# Set environment variables for Flask
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0
ENV FLASK_ENV=production

# Command to run the Flask app
CMD ["flask", "run", "--host=0.0.0.0", "--port=8080"]
