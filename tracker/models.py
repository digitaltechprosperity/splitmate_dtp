import uuid
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.db import models
from django.db.models.signals import post_save
from django.dispatch import receiver


class Expense(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    date = models.DateField()
    is_long_term = models.BooleanField(default=False)
    end_date = models.DateField(null=True, blank=True)
    interest_rate = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["-date", "-id"]

    def __str__(self):
        return self.name


class Friend(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="friends")
    name = models.CharField(max_length=100)
    email = models.EmailField()
    linked_user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="linked_friend_profiles",
    )

    class Meta:
        ordering = ["name"]
        unique_together = ("user", "email")

    def __str__(self):
        return f"{self.name} ({self.email})"

    @property
    def display_name(self):
        return self.name or self.email


class Group(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="owned_groups"
    )
    name = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)
    members = models.ManyToManyField(Friend, through="GroupMember")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    @property
    def owner(self):
        return self.user

    def get_member_record(self, app_user):
        if not app_user or not app_user.is_authenticated:
            return None
        return self.groupmember_set.filter(
            friend__linked_user=app_user
        ).select_related("friend").first()

    def get_user_role(self, app_user):
        if not app_user or not app_user.is_authenticated:
            return None

        if self.user_id == app_user.id:
            return GroupMember.ROLE_OWNER

        member_record = self.get_member_record(app_user)
        return member_record.role if member_record else None

    def is_owner(self, app_user):
        return self.get_user_role(app_user) == GroupMember.ROLE_OWNER

    def is_member(self, app_user):
        return self.get_user_role(app_user) in [
            GroupMember.ROLE_OWNER,
            GroupMember.ROLE_MEMBER,
        ]

    def can_view_group(self, app_user):
        return self.is_member(app_user)

    def can_edit_group(self, app_user):
        return self.is_owner(app_user)

    def can_delete_group(self, app_user):
        return self.is_owner(app_user)

    def can_manage_members(self, app_user):
        return self.is_owner(app_user)

    def can_create_split(self, app_user):
        return self.is_member(app_user)

    def can_mark_any_payment_settled(self, app_user):
        return self.is_member(app_user)

    def all_group_friends(self):
        return Friend.objects.filter(
            groupmember__group=self
        ).distinct()

    def active_members(self):
        return self.groupmember_set.filter(
            invite_status=GroupMember.STATUS_JOINED
        ).select_related("friend")

    def joined_friends(self):
        return Friend.objects.filter(
            groupmember__group=self,
            groupmember__invite_status=GroupMember.STATUS_JOINED
        ).distinct()

    def pending_friends(self):
        return Friend.objects.filter(
            groupmember__group=self,
            groupmember__invite_status=GroupMember.STATUS_PENDING
        ).distinct()


class GroupMember(models.Model):
    STATUS_PENDING = "pending"
    STATUS_JOINED = "joined"

    ROLE_OWNER = "owner"
    ROLE_MEMBER = "member"

    STATUS_CHOICES = (
        (STATUS_PENDING, "Pending"),
        (STATUS_JOINED, "Joined"),
    )

    ROLE_CHOICES = (
        (ROLE_OWNER, "Owner"),
        (ROLE_MEMBER, "Member"),
    )

    group = models.ForeignKey(Group, on_delete=models.CASCADE)
    friend = models.ForeignKey(Friend, on_delete=models.CASCADE)
    joined_at = models.DateTimeField(auto_now_add=True)
    invite_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_MEMBER)

    class Meta:
        unique_together = ("group", "friend")

    def __str__(self):
        return f"{self.group.name} - {self.friend.name} - {self.role}"

    @property
    def is_owner(self):
        return self.role == self.ROLE_OWNER

    @property
    def is_member(self):
        return self.role in [self.ROLE_OWNER, self.ROLE_MEMBER]


class GroupInvite(models.Model):
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="invites")
    invited_by = models.ForeignKey(User, on_delete=models.CASCADE)
    email = models.EmailField()
    invited_name = models.CharField(max_length=100, blank=True)
    invited_role = models.CharField(
        max_length=20,
        choices=GroupMember.ROLE_CHOICES,
        default=GroupMember.ROLE_MEMBER
    )
    is_accepted = models.BooleanField(default=False)
    accepted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="accepted_group_invites",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.email} -> {self.group.name}"


class SplitExpense(models.Model):
    SPLIT_TYPE_EQUAL = "equal"
    SPLIT_TYPE_CUSTOM = "custom"
    SPLIT_TYPE_PERCENTAGE = "percentage"

    PARTICIPANT_MODE_ALL = "all"
    PARTICIPANT_MODE_SELECTED = "selected"

    SPLIT_TYPE_CHOICES = (
        (SPLIT_TYPE_EQUAL, "Equal Split"),
        (SPLIT_TYPE_CUSTOM, "Custom Split"),
        (SPLIT_TYPE_PERCENTAGE, "Percentage Split"),
    )

    PARTICIPANT_MODE_CHOICES = (
        (PARTICIPANT_MODE_ALL, "All Members"),
        (PARTICIPANT_MODE_SELECTED, "Selected Members Only"),
    )

    group = models.ForeignKey(Group, on_delete=models.CASCADE, related_name="split_expenses")
    created_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="created_split_expenses"
    )
    title = models.CharField(max_length=200)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    paid_by = models.ForeignKey(
        Friend,
        on_delete=models.CASCADE,
        related_name="paid_expenses",
    )
    split_type = models.CharField(
        max_length=20,
        choices=SPLIT_TYPE_CHOICES,
        default=SPLIT_TYPE_EQUAL,
    )
    participant_mode = models.CharField(
        max_length=20,
        choices=PARTICIPANT_MODE_CHOICES,
        default=PARTICIPANT_MODE_ALL,
    )
    include_payer = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    expense_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    participants = models.ManyToManyField(
        Friend,
        through="SplitParticipant",
        related_name="participating_expenses",
        blank=True,
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    def clean(self):
        if self.total_amount is not None and self.total_amount <= 0:
            raise ValidationError("Total amount must be greater than zero.")

        if self.group_id and self.paid_by_id:
            if not GroupMember.objects.filter(
                group=self.group,
                friend=self.paid_by
            ).exists():
                raise ValidationError("Paid by user must belong to this group.")

    @property
    def settled_amount(self):
        total = self.shares.filter(is_settled=True).aggregate(total=models.Sum("share_amount"))["total"]
        return total or Decimal("0.00")

    @property
    def pending_amount(self):
        total = self.shares.filter(is_settled=False).aggregate(total=models.Sum("share_amount"))["total"]
        return total or Decimal("0.00")

    def can_edit(self, app_user):
        if not app_user or not app_user.is_authenticated:
            return False
        return self.group.is_owner(app_user)

    def can_delete(self, app_user):
        if not app_user or not app_user.is_authenticated:
            return False
        return self.group.is_owner(app_user)

    def can_mark_settlement(self, app_user):
        return self.group.can_mark_any_payment_settled(app_user)

    def get_selected_participants(self):
        return Friend.objects.filter(
            splitparticipant__expense=self,
            groupmember__group=self.group
        ).distinct()

    def get_final_participants(self):
        if self.participant_mode == self.PARTICIPANT_MODE_ALL:
            qs = self.group.all_group_friends()
        else:
            qs = self.get_selected_participants()

        if not self.include_payer:
            qs = qs.exclude(id=self.paid_by_id)

        return qs.distinct()

    def validate_participants(self):
        participants = self.get_final_participants()
        if not participants.exists():
            raise ValidationError("At least one participant is required for this split.")
        return participants


class SplitParticipant(models.Model):
    expense = models.ForeignKey(
        SplitExpense,
        on_delete=models.CASCADE,
        related_name="participant_rows"
    )
    friend = models.ForeignKey(
        Friend,
        on_delete=models.CASCADE,
        related_name="split_participant_rows"
    )

    class Meta:
        unique_together = ("expense", "friend")

    def __str__(self):
        return f"{self.expense.title} - {self.friend.name}"


class SplitShare(models.Model):
    expense = models.ForeignKey(
        SplitExpense,
        on_delete=models.CASCADE,
        related_name="shares",
    )
    friend = models.ForeignKey(Friend, on_delete=models.CASCADE)
    share_amount = models.DecimalField(max_digits=10, decimal_places=2)
    percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    is_settled = models.BooleanField(default=False)
    settled_at = models.DateTimeField(null=True, blank=True)
    settled_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="settled_split_shares"
    )

    class Meta:
        unique_together = ("expense", "friend")

    def __str__(self):
        return f"{self.friend.name} - {self.share_amount}"

    def clean(self):
        if self.share_amount is not None and self.share_amount < 0:
            raise ValidationError("Share amount cannot be negative.")

        if self.expense_id and self.friend_id:
            if not GroupMember.objects.filter(group=self.expense.group, friend=self.friend).exists():
                raise ValidationError("Share member must belong to the expense group.")

    def can_be_marked_settled_by(self, app_user):
        return self.expense.can_mark_settlement(app_user)


@receiver(post_save, sender=Group)
def auto_add_group_owner(sender, instance, created, **kwargs):
    if not created:
        return

    owner_user = instance.user

    owner_friend, _ = Friend.objects.get_or_create(
        user=owner_user,
        email=owner_user.email,
        defaults={
            "name": owner_user.username,
            "linked_user": owner_user,
        }
    )

    updated_fields = []

    if owner_friend.linked_user_id != owner_user.id:
        owner_friend.linked_user = owner_user
        updated_fields.append("linked_user")

    if not owner_friend.name:
        owner_friend.name = owner_user.username
        updated_fields.append("name")

    if updated_fields:
        owner_friend.save(update_fields=updated_fields)

    gm, _ = GroupMember.objects.get_or_create(
        group=instance,
        friend=owner_friend,
        defaults={
            "invite_status": GroupMember.STATUS_JOINED,
            "role": GroupMember.ROLE_OWNER,
        }
    )

    if gm.role != GroupMember.ROLE_OWNER or gm.invite_status != GroupMember.STATUS_JOINED:
        gm.role = GroupMember.ROLE_OWNER
        gm.invite_status = GroupMember.STATUS_JOINED
        gm.save(update_fields=["role", "invite_status"])


@receiver(post_save, sender=User)
def sync_owner_friend_records(sender, instance, created, **kwargs):
    if not instance.email:
        return

    owner_friend_qs = Friend.objects.filter(
        user=instance,
        email__iexact=instance.email
    )

    for friend in owner_friend_qs:
        changed = []

        if friend.linked_user_id != instance.id:
            friend.linked_user = instance
            changed.append("linked_user")

        if not friend.name:
            friend.name = instance.username
            changed.append("name")

        if changed:
            friend.save(update_fields=changed)