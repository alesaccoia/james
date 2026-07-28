from django.urls import path

from . import views

app_name = 'competitors'

urlpatterns = [
    path('', views.traffic, name='traffic'),
    path('data.json', views.data_traffic, name='data_traffic'),
    path('carica/', views.upload, name='upload'),
]
