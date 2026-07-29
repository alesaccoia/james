from django.urls import path

from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.compare, name='compare'),
    path('data/compare.json', views.data_compare, name='data_compare'),

    path('meta-ads/', views.dashboard, name='dashboard'),
    path('meta-ads/data.json', views.data_marketing, name='data_marketing'),

    path('facebook/', views.facebook_page, name='facebook'),
    path('facebook/data.json', views.data_facebook, name='data_facebook'),

    path('instagram/', views.instagram_page, name='instagram'),
    path('instagram/data.json', views.data_instagram, name='data_instagram'),

    path('meta-posts/', views.meta_posts, name='meta_posts'),
    path('meta-posts/data.json', views.data_meta_posts, name='data_meta_posts'),

    path('ga4/', views.ga4, name='ga4'),
    path('ga4/data.json', views.data_ga4, name='data_ga4'),

    path('eventi/', views.events, name='events'),
    path('eventi/data.json', views.data_events, name='data_events'),
    path('eventi/<int:pk>/elimina/', views.event_delete, name='event_delete'),

    path('funnel/', views.funnel, name='funnel'),
    path('funnel/data.json', views.data_funnel, name='data_funnel'),
]
