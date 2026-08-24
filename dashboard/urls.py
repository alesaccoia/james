from django.urls import path

from . import ingestion, ped_api, views

app_name = 'dashboard'

urlpatterns = [
    path('api/v1/editorial-calendar/', ped_api.editorial_calendar,
         name='editorial_calendar_api'),
    path('api/v1/editorial-calendar/<str:origin>/<str:ref>/',
         ped_api.editorial_calendar_item, name='editorial_calendar_item_api'),
    path('api/v1/ingest/events/', ingestion.ingest_events, name='ingest_events'),
    path('api/v1/ingest/snapshots/', ingestion.ingest_snapshots, name='ingest_snapshots'),
    path('', views.dashboard_redirect, name='root'),
    path('dashboard/', views.compare, name='compare'),
    path('guida/', views.help_page, name='help'),
    path('data/compare.json', views.data_compare, name='data_compare'),
    path('data/compare-presets.json', views.compare_presets, name='compare_presets'),
    path('data/home.json', views.data_home, name='data_home'),
    path('data/commercial-metrics.json', views.data_commercial_metrics,
         name='data_commercial_metrics'),
    path('data/performance-metrics.json', views.data_performance_metrics,
         name='data_performance_metrics'),
    path('commerciale/', views.commercial, name='commercial'),
    path('conversioni/', views.conversions, name='conversions'),

    path('meta-ads/', views.dashboard, name='dashboard'),
    path('meta-ads/data.json', views.data_marketing, name='data_marketing'),

    path('google-ads/', views.google_ads, name='google_ads'),
    path('google-ads/data.json', views.data_google_ads, name='data_google_ads'),

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

    path('pianificazione/', views.pianificazione, name='pianificazione'),
    path('pianificazione/data.json', views.data_pianificazione, name='data_pianificazione'),
    path('pianificazione/tag/nuovo/', views.tag_save, name='tag_save'),
    path('pianificazione/linea/salva/', views.budget_line_save, name='budget_line_save'),
    path('pianificazione/linea/<int:pk>/elimina/', views.budget_line_delete, name='budget_line_delete'),

    path('piani/', views.piani, name='piani'),
    path('piani/data.json', views.data_piani, name='data_piani'),
    path('piani/salva/', views.piano_save, name='piano_save'),
    path('piani/<int:pk>/elimina/', views.piano_delete, name='piano_delete'),

    path('tagging/', views.tagging, name='tagging'),
    path('tagging/data.json', views.data_tagging, name='data_tagging'),
    path('tagging/salva/', views.tagging_save, name='tagging_save'),

    path('calendario/', views.calendario, name='calendario'),
    path('calendario/data.json', views.data_calendario, name='data_calendario'),
    path('calendario/salva/', views.content_piece_save, name='content_piece_save'),
    path('calendario/<int:pk>/elimina/', views.content_piece_delete, name='content_piece_delete'),
]
