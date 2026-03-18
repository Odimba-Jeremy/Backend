from app import celery, app
import logging

if __name__ == '__main__':
    logging.info("🚀 Celery worker démarré")
    celery.start()
