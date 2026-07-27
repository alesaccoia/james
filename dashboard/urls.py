from django.urls import path

from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('data/marketing.json', views.data_marketing, name='data_marketing'),

    path('ga4/', views.ga4, name='ga4'),
    path('ga4/data.json', views.data_ga4, name='data_ga4'),

    path('confronto/', views.compare, name='compare'),
    path('confronto/data.json', views.data_compare, name='data_compare'),
]
