from django.urls import path

from . import views

app_name = 'competitors'

urlpatterns = [
    path('sov/', views.sov, name='sov'),
    path('sov/data.json', views.data_sov, name='data_sov'),
    path('', views.traffic, name='traffic'),
    path('data.json', views.data_traffic, name='data_traffic'),
    path('carica/', views.upload, name='upload'),
]
