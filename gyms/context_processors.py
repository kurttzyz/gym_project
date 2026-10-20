def branding(request):
    gym = getattr(request, "tenant", None)
    return {
        "tenant": gym,
        "branding_name": gym.name if gym else "Powerhouse",
        "branding_color": gym.primary_color if gym else "#245ac5",
        "branding_logo": gym.logo_url if gym else "",
    }
