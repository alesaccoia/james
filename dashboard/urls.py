from django.urls import path

from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.compare, name='compare'),
    path('data/compare.json', views.data_compare, name='data_compare'),

    path('meta-ads/', views.dashboard, name='dashboard'),
    path('meta-ads/data.json', views.data_marketing, name='data_marketing'),

    path('ga4/', views.ga4, name='ga4'),
    path('ga4/data.json', views.data_ga4, name='data_ga4'),

    path('eventi/', views.events, name='events'),
    path('eventi/data.json', views.data_events, name='data_events'),
    path('eventi/<int:pk>/elimina/', views.event_delete, name='event_delete'),
]
