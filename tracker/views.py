import json
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.contrib.auth.views import LoginView as DjangoLoginView
from django.contrib.auth.views import LogoutView as DjangoLogoutView
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import TruncMonth
from django.http import HttpResponseForbidden, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
import random
from twilio.rest import Client
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views import View
from django.views.generic import FormView, TemplateView

from .forms import ExpenseForm, FriendForm, GroupForm, RegisterForm, SplitExpenseForm
from .models import (
    Expense,
    Friend,
    Group,
    GroupInvite,
    GroupMember,
    SplitExpense,
    SplitParticipant,
    SplitShare,
)



def get_accessible_groups(user):
    return Group.objects.filter(
        Q(user=user) | Q(members__linked_user=user)
    ).distinct()


def get_group_for_user_or_404(user, group_id):
    return get_object_or_404(get_accessible_groups(user), id=group_id)


def get_group_friend_for_user(group, user):
    friend = Friend.objects.filter(
        groupmember__group=group,
        linked_user=user
    ).distinct().first()

    if friend:
        return friend

    if group.user_id == user.id:
        return Friend.objects.filter(
            user=group.user,
            email__iexact=user.email
        ).first()

    return None


def _store_pending_invite_token(request):
    token = request.GET.get("invite") or request.POST.get("invite")
    if token:
        request.session["pending_invite_token"] = token
    return token


def _forbidden():
    return HttpResponseForbidden("You do not have permission to perform this action.")
otp_storage = {}


def _round_money(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _save_selected_participants(expense, selected_participants):
    expense.participant_rows.all().delete()

    if expense.participant_mode != SplitExpense.PARTICIPANT_MODE_SELECTED:
        return

    valid_ids = set(expense.group.all_group_friends().values_list("id", flat=True))
    rows = []

    for friend in selected_participants:
        if friend.id in valid_ids:
            rows.append(SplitParticipant(expense=expense, friend=friend))

    if rows:
        SplitParticipant.objects.bulk_create(rows)


def _build_equal_shares(expense):
    participants = list(expense.validate_participants())
    member_count = len(participants)

    if member_count == 0:
        raise ValueError("Selected split has no participants.")

    total = _round_money(expense.total_amount)
    share = _round_money(total / member_count)

    created_shares = []
    total_created = Decimal("0.00")

    for index, member in enumerate(participants):
        amount = share
        if index == member_count - 1:
            amount = total - total_created

        created_shares.append(
            SplitShare(
                expense=expense,
                friend=member,
                share_amount=amount
            )
        )
        total_created += amount

    SplitShare.objects.bulk_create(created_shares)


def _build_custom_shares(expense, custom_data):
    try:
        parsed = json.loads(custom_data) if isinstance(custom_data, str) else custom_data
    except json.JSONDecodeError:
        raise ValueError("Invalid custom split format.")

    if not isinstance(parsed, list) or not parsed:
        raise ValueError("Custom split data must be a non-empty list.")

    allowed_members = {member.id: member for member in expense.get_final_participants()}
    created_shares = []
    total = Decimal("0.00")
    seen_friend_ids = set()

    for row in parsed:
        friend_id = int(row["friend_id"])
        amount = _round_money(str(row["amount"]))

        if friend_id not in allowed_members:
            raise ValueError("Custom split contains invalid participant.")

        if friend_id in seen_friend_ids:
            raise ValueError("Duplicate custom split participant found.")

        if amount <= 0:
            raise ValueError("Each custom share amount must be greater than zero.")

        seen_friend_ids.add(friend_id)
        created_shares.append(
            SplitShare(
                expense=expense,
                friend=allowed_members[friend_id],
                share_amount=amount
            )
        )
        total += amount

    if total != _round_money(expense.total_amount):
        raise ValueError("Custom split total must match total amount.")

    if set(allowed_members.keys()) != seen_friend_ids:
        raise ValueError("Custom split must include every selected participant.")

    SplitShare.objects.bulk_create(created_shares)


def _build_percentage_shares(expense, percentage_data):
    try:
        parsed = json.loads(percentage_data) if isinstance(percentage_data, str) else percentage_data
    except json.JSONDecodeError:
        raise ValueError("Invalid percentage split format.")

    if not isinstance(parsed, list) or not parsed:
        raise ValueError("Percentage split data must be a non-empty list.")

    allowed_members = {member.id: member for member in expense.get_final_participants()}
    created_shares = []
    total_amount = _round_money(expense.total_amount)
    percentage_total = Decimal("0.00")
    amount_total = Decimal("0.00")
    seen_friend_ids = set()
    rows = []

    for row in parsed:
        friend_id = int(row["friend_id"])
        percentage = _round_money(str(row["percentage"]))

        if friend_id not in allowed_members:
            raise ValueError("Percentage split contains invalid participant.")

        if friend_id in seen_friend_ids:
            raise ValueError("Duplicate percentage split participant found.")

        if percentage <= 0:
            raise ValueError("Each percentage must be greater than zero.")

        seen_friend_ids.add(friend_id)
        percentage_total += percentage
        rows.append((friend_id, percentage))

    if percentage_total != Decimal("100.00"):
        raise ValueError("Percentage total must be exactly 100.")

    for index, (friend_id, percentage) in enumerate(rows):
        amount = _round_money((total_amount * percentage) / Decimal("100"))
        if index == len(rows) - 1:
            amount = total_amount - amount_total

        created_shares.append(
            SplitShare(
                expense=expense,
                friend=allowed_members[friend_id],
                share_amount=amount,
                percentage=percentage
            )
        )
        amount_total += amount

    if set(allowed_members.keys()) != seen_friend_ids:
        raise ValueError("Percentage split must include every selected participant.")

    SplitShare.objects.bulk_create(created_shares)


def _rebuild_shares(expense, form):
    expense.shares.all().delete()

    if expense.split_type == SplitExpense.SPLIT_TYPE_EQUAL:
        _build_equal_shares(expense)
    elif expense.split_type == SplitExpense.SPLIT_TYPE_CUSTOM:
        _build_custom_shares(expense, form.cleaned_data.get("custom_split_data"))
    elif expense.split_type == SplitExpense.SPLIT_TYPE_PERCENTAGE:
        _build_percentage_shares(expense, form.cleaned_data.get("percentage_split_data"))
    else:
        raise ValueError("Invalid split type.")


def send_split_expense_emails(expense):
    shares = expense.shares.select_related("friend").all()

    for share in shares:
        if share.friend_id == expense.paid_by_id:
            continue

        recipient_email = getattr(share.friend, "email", None)
        if not recipient_email:
            continue

        subject = f"New split expense: {expense.title}"
        message = (
            f"Hello {share.friend.name},\n\n"
            f"A new split expense has been added.\n\n"
            f"Title: {expense.title}\n"
            f"Group: {expense.group.name}\n"
            f"Paid by: {expense.paid_by.name}\n"
            f"Your share: ₹{share.share_amount}\n"
            f"Split type: {expense.get_split_type_display()}\n\n"
            f"Please settle this amount when convenient.\n\n"
            f"- Expense Tracker"
        )

        try:
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [recipient_email],
                fail_silently=True,
            )
        except Exception:
            pass


def send_group_invite_email(invite, request):
    invite_url = request.build_absolute_uri(
        reverse("accept_invite", kwargs={"token": invite.token})
    )

    subject = f"You were invited to join {invite.group.name}"
    message = (
        f"Hello {invite.invited_name or 'there'},\n\n"
        f"You were invited to join the group '{invite.group.name}'.\n\n"
        f"Click the link below to join:\n{invite_url}\n\n"
        f"If you already have an account, login using the same invited email.\n"
        f"If you do not have an account, register first using the same invited email.\n\n"
        f"- Expense Tracker"
    )

    send_mail(
        subject,
        message,
        settings.DEFAULT_FROM_EMAIL,
        [invite.email],
        fail_silently=False,
    )


def _attach_member_to_group(group, member, request):
    existing_user = User.objects.filter(email__iexact=member.email).first()

    if existing_user and member.linked_user_id != existing_user.id:
        member.linked_user = existing_user
        member.save(update_fields=["linked_user"])

    gm, _ = GroupMember.objects.get_or_create(group=group, friend=member)
    gm.role = GroupMember.ROLE_MEMBER

    if existing_user:
        gm.invite_status = GroupMember.STATUS_JOINED
        gm.save(update_fields=["invite_status", "role"])

        invite = GroupInvite.objects.filter(
            group=group,
            email__iexact=member.email,
            is_accepted=False,
        ).first()
        if invite:
            invite.is_accepted = True
            invite.accepted_by = existing_user
            invite.accepted_at = timezone.now()
            invite.invited_role = GroupMember.ROLE_MEMBER
            invite.save(update_fields=["is_accepted", "accepted_by", "accepted_at", "invited_role"])
        return

    gm.invite_status = GroupMember.STATUS_PENDING
    gm.save(update_fields=["invite_status", "role"])

    invite = GroupInvite.objects.filter(
        group=group,
        email__iexact=member.email,
    ).order_by("-created_at").first()

    if not invite:
        invite = GroupInvite.objects.create(
            group=group,
            invited_by=request.user,
            email=member.email,
            invited_name=member.name,
            invited_role=GroupMember.ROLE_MEMBER,
        )
    else:
        invite.invited_role = GroupMember.ROLE_MEMBER
        invite.invited_name = member.name
        invite.save(update_fields=["invited_role", "invited_name"])

    if not invite.is_accepted:
        send_group_invite_email(invite, request)


def _sync_group_members(group, selected_members, request):
    selected_member_ids = set(member.id for member in selected_members)

    GroupMember.objects.filter(group=group).exclude(
        role=GroupMember.ROLE_OWNER
    ).exclude(
        friend_id__in=selected_member_ids
    ).delete()

    for member in selected_members:
        _attach_member_to_group(group, member, request)

    owner_gm = group.groupmember_set.filter(role=GroupMember.ROLE_OWNER).first()
    if owner_gm:
        owner_gm.role = GroupMember.ROLE_OWNER
        owner_gm.invite_status = GroupMember.STATUS_JOINED
        owner_gm.save(update_fields=["role", "invite_status"])


def _get_group_settlements(group):
    balances = defaultdict(Decimal)
    friend_names = {}

    expenses = (
        SplitExpense.objects.filter(group=group)
        .select_related("paid_by", "group")
        .prefetch_related("shares__friend")
        .order_by("-created_at")
    )

    for expense in expenses:
        friend_names[expense.paid_by.id] = expense.paid_by.name
        for share in expense.shares.all():
            friend_names[share.friend.id] = share.friend.name
            if share.friend != expense.paid_by and not share.is_settled:
                balances[(share.friend.id, expense.paid_by.id)] += share.share_amount

    return [
        {
            "debtor": friend_names.get(debtor_id, "Unknown"),
            "creditor": friend_names.get(creditor_id, "Unknown"),
            "amount": amount
        }
        for (debtor_id, creditor_id), amount in balances.items()
        if amount > 0
    ]


def _get_user_settlements(user):
    groups = get_accessible_groups(user)
    balances = defaultdict(Decimal)
    friend_names = {}

    expenses = (
        SplitExpense.objects.filter(group__in=groups)
        .select_related("paid_by", "group")
        .prefetch_related("shares__friend")
        .order_by("-created_at")
    )

    for expense in expenses:
        friend_names[expense.paid_by.id] = expense.paid_by.name
        for share in expense.shares.all():
            friend_names[share.friend.id] = share.friend.name
            if share.friend != expense.paid_by and not share.is_settled:
                balances[(share.friend.id, expense.paid_by.id)] += share.share_amount

    return [
        {
            "debtor": friend_names.get(debtor_id, "Unknown"),
            "creditor": friend_names.get(creditor_id, "Unknown"),
            "amount": amount
        }
        for (debtor_id, creditor_id), amount in balances.items()
        if amount > 0
    ]


class HomeView(TemplateView):
    template_name = "home.html"


class RegisterView(FormView):
    template_name = "register.html"
    form_class = RegisterForm
    success_url = reverse_lazy("dashboard")

    def dispatch(self, request, *args, **kwargs):
        _store_pending_invite_token(request)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["invite_token"] = self.request.session.get("pending_invite_token", "")
        return context

    def form_valid(self, form):
        user = form.save()
        login(self.request, user)

        token = self.request.session.pop("pending_invite_token", None)
        if token:
            return redirect("accept_invite", token=token)

        return redirect("dashboard")


class CustomLoginView(DjangoLoginView):
    template_name = "login.html"

    def dispatch(self, request, *args, **kwargs):
        _store_pending_invite_token(request)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["invite_token"] = self.request.session.get("pending_invite_token", "")
        return context

    def get_success_url(self):
        token = self.request.session.pop("pending_invite_token", None)
        if token:
            return reverse("accept_invite", kwargs={"token": token})
        return reverse("dashboard")


class CustomLogoutView(DjangoLogoutView):
    next_page = reverse_lazy("home")


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard.html"
    login_url = reverse_lazy("login")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        expenses = Expense.objects.filter(user=self.request.user).order_by("-date")

        monthly = (
            Expense.objects.filter(user=self.request.user)
            .annotate(month=TruncMonth("date"))
            .values("month")
            .annotate(total=Sum("amount"))
            .order_by("month")
        )

        labels = []
        data = []

        for row in monthly:
            labels.append(row["month"].strftime("%b %Y"))
            data.append(float(row["total"]))

        accessible_groups = get_accessible_groups(self.request.user)

        context.update({
            "form": ExpenseForm(),
            "expenses": expenses,
            "labels": json.dumps(labels),
            "data": json.dumps(data),
            "total_personal": Expense.objects.filter(user=self.request.user).aggregate(total=Sum("amount"))["total"] or 0,
            "total_groups": accessible_groups.count(),
            "total_friends": Friend.objects.filter(user=self.request.user).count(),
            "total_split_expenses": SplitExpense.objects.filter(group__in=accessible_groups).count(),
        })
        return context

    def post(self, request, *args, **kwargs):
        form = ExpenseForm(request.POST)
        if form.is_valid():
            expense = form.save(commit=False)
            expense.user = request.user
            expense.save()
            return redirect("dashboard")

        context = self.get_context_data()
        context["form"] = form
        return self.render_to_response(context)


class FriendListCreateView(LoginRequiredMixin, TemplateView):
    template_name = "friends.html"
    login_url = reverse_lazy("login")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query = self.request.GET.get("q", "").strip()

        friends = Friend.objects.filter(user=self.request.user)
        if query:
            friends = friends.filter(Q(name__icontains=query) | Q(email__icontains=query))

        context.update({
            "friends": friends.order_by("name"),
            "form": kwargs.get("form", FriendForm(user=self.request.user)),
            "query": query,
        })
        return context

    def post(self, request, *args, **kwargs):
        form = FriendForm(request.POST, user=request.user)
        if form.is_valid():
            friend = form.save(commit=False)
            friend.user = request.user

            existing_user = User.objects.filter(email__iexact=friend.email).first()
            if existing_user:
                friend.linked_user = existing_user

            friend.save()
            return redirect("friends_list")

        context = self.get_context_data(form=form)
        return self.render_to_response(context)


class FriendUpdateView(LoginRequiredMixin, TemplateView):
    template_name = "friends.html"
    login_url = reverse_lazy("login")

    def dispatch(self, request, *args, **kwargs):
        self.friend = get_object_or_404(Friend, id=kwargs["friend_id"], user=request.user)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        query = self.request.GET.get("q", "").strip()

        friends = Friend.objects.filter(user=self.request.user)
        if query:
            friends = friends.filter(Q(name__icontains=query) | Q(email__icontains=query))

        context.update({
            "friends": friends.order_by("name"),
            "form": kwargs.get("form", FriendForm(instance=self.friend, user=self.request.user)),
            "query": query,
            "editing_friend": self.friend,
        })
        return context

    def post(self, request, *args, **kwargs):
        form = FriendForm(request.POST, instance=self.friend, user=request.user)
        if form.is_valid():
            friend = form.save(commit=False)
            friend.user = request.user

            existing_user = User.objects.filter(email__iexact=friend.email).first()
            friend.linked_user = existing_user if existing_user else None
            friend.save()

            return redirect("friends_list")

        context = self.get_context_data(form=form)
        return self.render_to_response(context)


class FriendDeleteView(LoginRequiredMixin, View):
    login_url = reverse_lazy("login")

    def post(self, request, friend_id):
        friend = get_object_or_404(Friend, id=friend_id, user=request.user)
        friend.delete()
        return redirect("friends_list")

    def get(self, request, *args, **kwargs):
        return HttpResponseNotAllowed(["POST"])


class GroupListCreateView(LoginRequiredMixin, TemplateView):
    template_name = "groups.html"
    login_url = reverse_lazy("login")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        groups = get_accessible_groups(self.request.user)
        group_data = []

        for group in groups:
            members_count = GroupMember.objects.filter(group=group).count()
            total_amount = SplitExpense.objects.filter(group=group).aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")
            joined_count = GroupMember.objects.filter(group=group, invite_status=GroupMember.STATUS_JOINED).count()
            pending_count = GroupMember.objects.filter(group=group, invite_status=GroupMember.STATUS_PENDING).count()

            group_data.append({
                "group": group,
                "members_count": members_count,
                "total_amount": total_amount,
                "joined_count": joined_count,
                "pending_count": pending_count,
                "is_owner": group.is_owner(self.request.user),
                "can_edit": group.can_edit_group(self.request.user),
                "can_delete": group.can_delete_group(self.request.user),
            })

        context.update({
            "groups": group_data,
            "form": kwargs.get("form", GroupForm(user=self.request.user)),
            "friends_count": Friend.objects.filter(user=self.request.user).count(),
        })
        return context

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        form = GroupForm(request.POST, user=request.user)
        if form.is_valid():
            try:
                group = Group.objects.create(
                    user=request.user,
                    name=form.cleaned_data["name"]
                )

                _sync_group_members(
                    group=group,
                    selected_members=form.cleaned_data["members"],
                    request=request,
                )

                messages.success(request, "Group created successfully.")
                return redirect("group_detail", group_id=group.id)
            except Exception as exc:
                transaction.set_rollback(True)
                form.add_error(None, f"Unable to create group or send invite: {exc}")

        context = self.get_context_data(form=form)
        return self.render_to_response(context)


class GroupUpdateView(LoginRequiredMixin, TemplateView):
    template_name = "groups.html"
    login_url = reverse_lazy("login")

    def dispatch(self, request, *args, **kwargs):
        self.group = get_object_or_404(Group, id=kwargs["group_id"])
        if not self.group.can_edit_group(request.user):
            return _forbidden()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        groups = get_accessible_groups(self.request.user)
        group_data = []

        for item in groups:
            members_count = GroupMember.objects.filter(group=item).count()
            total_amount = SplitExpense.objects.filter(group=item).aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")
            joined_count = GroupMember.objects.filter(group=item, invite_status=GroupMember.STATUS_JOINED).count()
            pending_count = GroupMember.objects.filter(group=item, invite_status=GroupMember.STATUS_PENDING).count()

            group_data.append({
                "group": item,
                "members_count": members_count,
                "total_amount": total_amount,
                "joined_count": joined_count,
                "pending_count": pending_count,
                "is_owner": item.is_owner(self.request.user),
                "can_edit": item.can_edit_group(self.request.user),
                "can_delete": item.can_delete_group(self.request.user),
            })

        initial_members = Friend.objects.filter(
            groupmember__group=self.group
        ).exclude(
            groupmember__role=GroupMember.ROLE_OWNER
        ).distinct()

        context.update({
            "groups": group_data,
            "form": kwargs.get(
                "form",
                GroupForm(
                    user=self.request.user,
                    instance=self.group,
                    initial={"members": initial_members}
                )
            ),
            "friends_count": Friend.objects.filter(user=self.request.user).count(),
            "editing_group": self.group,
        })
        return context

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        form = GroupForm(request.POST, user=request.user, instance=self.group)
        if form.is_valid():
            try:
                self.group.name = form.cleaned_data["name"]
                self.group.save()

                _sync_group_members(
                    group=self.group,
                    selected_members=form.cleaned_data["members"],
                    request=request,
                )

                messages.success(request, "Group updated successfully.")
                return redirect("groups_list")
            except Exception as exc:
                transaction.set_rollback(True)
                form.add_error(None, f"Unable to update group or send invite: {exc}")

        context = self.get_context_data(form=form)
        return self.render_to_response(context)


class GroupDeleteView(LoginRequiredMixin, View):
    login_url = reverse_lazy("login")

    def post(self, request, group_id):
        group = get_object_or_404(Group, id=group_id)
        if not group.can_delete_group(request.user):
            return _forbidden()
        group.delete()
        return redirect("groups_list")

    def get(self, request, *args, **kwargs):
        return HttpResponseNotAllowed(["POST"])


class GroupDetailView(LoginRequiredMixin, TemplateView):
    template_name = "group_detail.html"
    login_url = reverse_lazy("login")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        group = get_group_for_user_or_404(self.request.user, kwargs["group_id"])
        members = Friend.objects.filter(groupmember__group=group).distinct().order_by("name")
        expenses = SplitExpense.objects.filter(group=group).order_by("-created_at").prefetch_related(
            "shares__friend",
            "participant_rows__friend"
        )

        total_group_amount = expenses.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")
        expense_rows = []

        for expense in expenses:
            shares = list(expense.shares.all())

            if expense.participant_mode == SplitExpense.PARTICIPANT_MODE_ALL:
                participant_names = list(
                    group.all_group_friends().order_by("name").values_list("name", flat=True)
                )
                if not expense.include_payer and expense.paid_by.name in participant_names:
                    participant_names = [name for name in participant_names if name != expense.paid_by.name]
            else:
                participant_names = [
                    participant.friend.name
                    for participant in expense.participant_rows.select_related("friend").all()
                ]
                if not expense.include_payer and expense.paid_by.name in participant_names:
                    participant_names = [name for name in participant_names if name != expense.paid_by.name]

            equal_share = "-"
            if shares and expense.split_type == SplitExpense.SPLIT_TYPE_EQUAL:
                equal_share = shares[0].share_amount

            settled_amount = sum((share.share_amount for share in shares if share.is_settled), Decimal("0.00"))
            pending_amount = sum((share.share_amount for share in shares if not share.is_settled), Decimal("0.00"))

            share_rows = []
            for share in shares:
                share_rows.append({
                    "share": share,
                    "can_mark": share.can_be_marked_settled_by(self.request.user),
                })

            expense_rows.append({
                "expense": expense,
                "title": expense.title,
                "paid_by": expense.paid_by.name,
                "total_amount": expense.total_amount,
                "per_person": equal_share,
                "split_type": expense.split_type,
                "participant_mode": expense.participant_mode,
                "participant_names": participant_names,
                "created_at": expense.created_at,
                "shares": share_rows,
                "settled_amount": settled_amount,
                "pending_amount": pending_amount,
                "can_edit": expense.can_edit(self.request.user),
                "can_delete": expense.can_delete(self.request.user),
            })

        member_status = GroupMember.objects.filter(group=group).select_related("friend").order_by("friend__name")

        context.update({
            "group": group,
            "members": members,
            "member_status": member_status,
            "expenses": expense_rows,
            "total_group_amount": total_group_amount,
            "member_count": members.count(),
            "settlements": _get_group_settlements(group),
            "can_edit_group": group.can_edit_group(self.request.user),
            "can_delete_group": group.can_delete_group(self.request.user),
            "can_create_split": group.can_create_split(self.request.user),
            "can_mark_any_payment": group.can_mark_any_payment_settled(self.request.user),
            "current_user_role": group.get_user_role(self.request.user),
        })
        return context


class GroupMembersAPIView(LoginRequiredMixin, View):
    login_url = reverse_lazy("login")

    def get(self, request, group_id):
        group = get_group_for_user_or_404(request.user, group_id)
        members = Friend.objects.filter(
            groupmember__group=group
        ).distinct().order_by("name").values("id", "name", "email")
        return JsonResponse(list(members), safe=False)


class InviteAcceptView(View):
    @transaction.atomic
    def get(self, request, token):
        invite = get_object_or_404(GroupInvite, token=token)

        if invite.is_accepted:
            if request.user.is_authenticated:
                messages.info(request, "This invite was already accepted.")
                return redirect("dashboard")
            messages.info(request, "This invite has already been accepted.")
            return redirect("login")

        request.session["pending_invite_token"] = str(invite.token)

        if not request.user.is_authenticated:
            return render(request, "invite_landing.html", {
                "invite": invite,
                "email_matches": False,
                "is_logged_in": False,
            })

        current_email = (request.user.email or "").strip().lower()
        invited_email = (invite.email or "").strip().lower()

        if not current_email or current_email != invited_email:
            return render(request, "invite_landing.html", {
                "invite": invite,
                "email_matches": False,
                "is_logged_in": True,
                "current_email": request.user.email,
            })

        friend = Friend.objects.filter(
            user=invite.group.user,
            email__iexact=invite.email,
        ).first()

        if not friend:
            friend = Friend.objects.create(
                user=invite.group.user,
                name=invite.invited_name or request.user.username,
                email=invite.email,
                linked_user=request.user,
            )
        else:
            updated_fields = []

            if friend.linked_user_id != request.user.id:
                friend.linked_user = request.user
                updated_fields.append("linked_user")

            if not friend.name and (invite.invited_name or request.user.username):
                friend.name = invite.invited_name or request.user.username
                updated_fields.append("name")

            if updated_fields:
                friend.save(update_fields=updated_fields)

        gm, _ = GroupMember.objects.get_or_create(group=invite.group, friend=friend)
        gm.role = GroupMember.ROLE_MEMBER
        if gm.invite_status != GroupMember.STATUS_JOINED:
            gm.invite_status = GroupMember.STATUS_JOINED
        gm.save(update_fields=["invite_status", "role"])

        invite.is_accepted = True
        invite.accepted_by = request.user
        invite.accepted_at = timezone.now()
        invite.save(update_fields=["is_accepted", "accepted_by", "accepted_at"])

        request.session.pop("pending_invite_token", None)
        messages.success(request, f"You have joined '{invite.group.name}'.")
        return redirect("group_detail", group_id=invite.group.id)


class SplitExpenseListCreateView(LoginRequiredMixin, TemplateView):
    template_name = "split_expense.html"
    login_url = reverse_lazy("login")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        accessible_groups = get_accessible_groups(self.request.user)
        expenses = SplitExpense.objects.filter(group__in=accessible_groups).order_by("-created_at").prefetch_related(
            "shares__friend",
            "participant_rows__friend"
        )

        expense_data = []
        for expense in expenses:
            shares = list(expense.shares.all())
            per_person = shares[0].share_amount if shares and expense.split_type == SplitExpense.SPLIT_TYPE_EQUAL else "-"
            expense_data.append({
                "expense": expense,
                "per_person": per_person,
                "member_count": len(shares),
                "can_edit": expense.can_edit(self.request.user),
                "can_delete": expense.can_delete(self.request.user),
            })

        context.update({
            "form": kwargs.get("form", SplitExpenseForm(user=self.request.user)),
            "expenses": expense_data,
            "editing": False,
            "custom_share_json": "[]",
            "percentage_share_json": "[]",
            "selected_participant_ids": "[]",
        })
        return context

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        form = SplitExpenseForm(request.POST, user=request.user)
        if form.is_valid():
            group = get_group_for_user_or_404(request.user, form.cleaned_data["group"].id)

            if not group.can_create_split(request.user):
                return _forbidden()

            expense = form.save(commit=False)
            expense.group = group
            expense.created_by = request.user

            friend = get_group_friend_for_user(group, request.user)
            if not friend:
                messages.error(request, "User is not linked to any group member profile.")
                return redirect("split_expense")

            expense.paid_by = friend
            expense.save()

            selected_participants = form.cleaned_data.get("selected_participants")
            _save_selected_participants(expense, selected_participants)

            try:
                _rebuild_shares(expense, form)
                send_split_expense_emails(expense)
                messages.success(request, "Split expense created successfully.")
                return redirect("group_detail", group_id=expense.group.id)
            except Exception as exc:
                transaction.set_rollback(True)
                expense.delete()
                form.add_error(None, str(exc))

        context = self.get_context_data(form=form)
        return self.render_to_response(context)


class SplitExpenseUpdateView(LoginRequiredMixin, TemplateView):
    template_name = "split_expense.html"
    login_url = reverse_lazy("login")

    def dispatch(self, request, *args, **kwargs):
        self.expense = get_object_or_404(
            SplitExpense.objects.filter(
                Q(group__user=request.user) | Q(group__members__linked_user=request.user)
            ).distinct(),
            id=kwargs["expense_id"]
        )
        if not self.expense.can_edit(request.user):
            return _forbidden()
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        accessible_groups = get_accessible_groups(self.request.user)
        expenses = SplitExpense.objects.filter(group__in=accessible_groups).order_by("-created_at").prefetch_related(
            "shares__friend",
            "participant_rows__friend"
        )

        expense_data = []
        for item in expenses:
            shares = list(item.shares.all())
            per_person = shares[0].share_amount if shares and item.split_type == SplitExpense.SPLIT_TYPE_EQUAL else "-"
            expense_data.append({
                "expense": item,
                "per_person": per_person,
                "member_count": len(shares),
                "can_edit": item.can_edit(self.request.user),
                "can_delete": item.can_delete(self.request.user),
            })

        custom_share_json = json.dumps([
            {
                "friend_id": share.friend.id,
                "name": share.friend.name,
                "amount": str(share.share_amount)
            }
            for share in self.expense.shares.all()
        ])

        percentage_share_json = json.dumps([
            {
                "friend_id": share.friend.id,
                "name": share.friend.name,
                "percentage": str(share.percentage or "0.00")
            }
            for share in self.expense.shares.all()
        ])

        selected_participant_ids = json.dumps(
            list(self.expense.get_selected_participants().values_list("id", flat=True))
        )

        context.update({
            "form": kwargs.get("form", SplitExpenseForm(user=self.request.user, group=self.expense.group, instance=self.expense)),
            "expenses": expense_data,
            "editing": True,
            "edit_expense": self.expense,
            "custom_share_json": custom_share_json,
            "percentage_share_json": percentage_share_json,
            "selected_participant_ids": selected_participant_ids,
        })
        return context

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        form = SplitExpenseForm(request.POST, user=request.user, instance=self.expense)
        if form.is_valid():
            group = get_group_for_user_or_404(request.user, form.cleaned_data["group"].id)

            if not self.expense.can_edit(request.user):
                return _forbidden()

            updated_expense = form.save(commit=False)
            updated_expense.group = group

            friend = get_group_friend_for_user(group, request.user)
            if not friend:
                messages.error(request, "User is not linked to any group member profile.")
                return redirect("split_expense")

            updated_expense.paid_by = friend
            updated_expense.save()

            selected_participants = form.cleaned_data.get("selected_participants")
            _save_selected_participants(updated_expense, selected_participants)

            try:
                _rebuild_shares(updated_expense, form)
                send_split_expense_emails(updated_expense)
                messages.success(request, "Split expense updated successfully.")
                return redirect("group_detail", group_id=updated_expense.group.id)
            except Exception as exc:
                transaction.set_rollback(True)
                form.add_error(None, str(exc))

        context = self.get_context_data(form=form)
        return self.render_to_response(context)


class SplitExpenseDeleteView(LoginRequiredMixin, View):
    login_url = reverse_lazy("login")

    def post(self, request, expense_id):
        expense = get_object_or_404(
            SplitExpense.objects.filter(
                Q(group__user=request.user) | Q(group__members__linked_user=request.user)
            ).distinct(),
            id=expense_id
        )

        if not expense.can_delete(request.user):
            return _forbidden()

        group_id = expense.group.id
        expense.delete()
        messages.success(request, "Split expense deleted successfully.")
        return redirect("group_detail", group_id=group_id)

    def get(self, request, *args, **kwargs):
        return HttpResponseNotAllowed(["POST"])


class MarkShareSettledView(LoginRequiredMixin, View):
    login_url = reverse_lazy("login")

    def post(self, request, share_id):
        share = get_object_or_404(
            SplitShare.objects.select_related("expense__group").filter(
                Q(expense__group__user=request.user) | Q(expense__group__members__linked_user=request.user)
            ).distinct(),
            id=share_id
        )

        if not share.can_be_marked_settled_by(request.user):
            return _forbidden()

        share.is_settled = True
        share.settled_at = timezone.now()
        share.settled_by = request.user
        share.save(update_fields=["is_settled", "settled_at", "settled_by"])

        messages.success(request, "Payment marked as settled.")
        return redirect("group_detail", group_id=share.expense.group.id)


class SettlementsView(LoginRequiredMixin, TemplateView):
    template_name = "settlements.html"
    login_url = reverse_lazy("login")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["settlements"] = _get_user_settlements(self.request.user)
        return context
    
def send_otp(request):
    if request.method == "POST":
        phone = request.POST.get("phone")

        otp = str(random.randint(100000, 999999))
        otp_storage[phone] = otp

        try:
            client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

            message = client.messages.create(
                body=f"Your OTP is {otp}",
                from_=settings.TWILIO_PHONE_NUMBER,
                to=phone
            )

            print("✅ OTP SENT:", message.sid)

        except Exception as e:
            print("🔥 ERROR SENDING OTP:", e)
            return render(request, "send_otp.html", {"error": str(e)})

        return render(request, "verify.html", {"phone": phone})

    return render(request, "send_otp.html")
  
def verify_otp(request):
    if request.method == "POST":
        phone = request.POST.get("phone")
        entered_otp = request.POST.get("otp")

        if otp_storage.get(phone) == entered_otp:
            user, created = User.objects.get_or_create(username=phone)
            user.backend = "django.contrib.auth.backends.ModelBackend"
            login(request, user)
            del otp_storage[phone]
            return redirect("dashboard")

        return render(request, "verify.html", {
            "phone": phone,
            "error": "Invalid OTP"
        })

