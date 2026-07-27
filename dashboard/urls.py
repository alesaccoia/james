from django.urls import path

from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('data/marketing.json', views.data_marketing, name='data_marketing'),
]
