from django.urls import path
from .views import (
    HomeView,
    RegisterView,
    CustomLoginView,
    CustomLogoutView,
    DashboardView,
    FriendListCreateView,
    FriendUpdateView,
    FriendDeleteView,
    GroupListCreateView,
    GroupUpdateView,
    GroupDeleteView,
    GroupDetailView,
    GroupMembersAPIView,
    InviteAcceptView,
    SplitExpenseListCreateView,
    SplitExpenseUpdateView,
    SplitExpenseDeleteView,
    MarkShareSettledView,
    SettlementsView,
    send_otp,
    verify_otp,
    set_password,
    forgot_password,
    
    
)



urlpatterns = [
    path("", HomeView.as_view(), name="home"),
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", CustomLoginView.as_view(template_name="login.html"), name="login"),
    path("logout/", CustomLogoutView.as_view(), name="logout"),

    path("dashboard/", DashboardView.as_view(), name="dashboard"),

    path("friends/", FriendListCreateView.as_view(), name="friends_list"),
    path("friends/edit/<int:friend_id>/", FriendUpdateView.as_view(), name="edit_friend"),
    path("friends/delete/<int:friend_id>/", FriendDeleteView.as_view(), name="delete_friend"),

    path("groups/", GroupListCreateView.as_view(), name="groups_list"),
    path("groups/<int:group_id>/", GroupDetailView.as_view(), name="group_detail"),
    path("groups/<int:group_id>/members/", GroupMembersAPIView.as_view(), name="group_members_api"),
    path("groups/edit/<int:group_id>/", GroupUpdateView.as_view(), name="edit_group"),
    path("groups/delete/<int:group_id>/", GroupDeleteView.as_view(), name="delete_group"),

    path("invite/<uuid:token>/", InviteAcceptView.as_view(), name="accept_invite"),

    path("split-expense/", SplitExpenseListCreateView.as_view(), name="split_expense"),
    path("split-expense/edit/<int:expense_id>/", SplitExpenseUpdateView.as_view(), name="edit_split_expense"),
    path("split-expense/delete/<int:expense_id>/", SplitExpenseDeleteView.as_view(), name="delete_split_expense"),

    path("shares/<int:share_id>/settle/", MarkShareSettledView.as_view(), name="mark_share_settled"),

    path("settlements/", SettlementsView.as_view(), name="settlements"),
    path('send-otp/', send_otp, name='send_otp'),
    path('verify-otp/', verify_otp, name='verify_otp'),

    path("forgot-password/", forgot_password, name="forgot_password"),
    path("set-password/<uuid:token>/", set_password, name="set_password"),

]