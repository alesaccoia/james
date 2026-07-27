"""
Django settings for james project.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

SECRET_KEY = os.environ.get(
    'SECRET_KEY', 'django-insecure-local-dev-only-change-me')

DEBUG = os.environ.get('DEBUG', 'true').lower() == 'true'

ALLOWED_HOSTS = [h.strip() for h in
                 os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')
                 if h.strip()]

CSRF_TRUSTED_ORIGINS = [o.strip() for o in
                        os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',')
                        if o.strip()]


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'dashboard',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'james.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'james.wsgi.application'


# Database
# 'default' holds James's own app data (auth, sessions, dashboard models).
# 'airbyte' is a read-only-in-practice connection to the raw landing zone
# that Airbyte writes into (_airbyte_raw_<stream> tables) — kept as a
# separate database so this project stays reusable for other Airbyte
# destinations without touching app data.
#
# Locally, with no DB_HOST set, 'default' falls back to sqlite so the app
# can be run/tested without a live Postgres; the 'airbyte' alias is only
# wired up when DB_HOST is set (i.e. in real deployments).

DB_HOST = os.environ.get('DB_HOST')

if DB_HOST:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'HOST': DB_HOST,
            'PORT': os.environ.get('DB_PORT', '5432'),
            'NAME': os.environ.get('DB_NAME', 'james'),
            'USER': os.environ.get('DB_USER', 'airbyte_writer'),
            'PASSWORD': os.environ.get('DB_PASSWORD', ''),
        },
        'airbyte': {
            'ENGINE': 'django.db.backends.postgresql',
            'HOST': DB_HOST,
            'PORT': os.environ.get('DB_PORT', '5432'),
            'NAME': os.environ.get('AIRBYTE_DB_NAME', 'airbyte_raw'),
            'USER': os.environ.get('DB_USER', 'airbyte_writer'),
            'PASSWORD': os.environ.get('DB_PASSWORD', ''),
        },
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Europe/Rome'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

LOGIN_URL = 'login'
LOGIN_REDIRECT_URL = 'dashboard:dashboard'
