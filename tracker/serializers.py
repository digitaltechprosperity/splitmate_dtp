from django.contrib.auth.models import User
from django.db.models import Sum
from rest_framework import serializers

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


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email"]


class ExpenseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Expense
        fields = [
            "id",
            "user",
            "name",
            "amount",
            "date",
            "is_long_term",
            "end_date",
            "interest_rate",
        ]
        read_only_fields = ["id", "user"]


class FriendSerializer(serializers.ModelSerializer):
    linked_user = UserSerializer(read_only=True)

    class Meta:
        model = Friend
        fields = ["id", "user", "name", "email", "linked_user"]
        read_only_fields = ["id", "user", "linked_user"]


class GroupMemberSerializer(serializers.ModelSerializer):
    friend = FriendSerializer(read_only=True)

    class Meta:
        model = GroupMember
        fields = ["id", "friend", "invite_status", "role", "joined_at"]


class GroupInviteSerializer(serializers.ModelSerializer):
    class Meta:
        model = GroupInvite
        fields = [
            "id",
            "token",
            "group",
            "invited_by",
            "email",
            "invited_name",
            "invited_role",
            "is_accepted",
            "accepted_by",
            "created_at",
            "accepted_at",
        ]


class GroupSerializer(serializers.ModelSerializer):
    member_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        write_only=True,
        required=False,
        default=list
    )

    class Meta:
        model = Group
        fields = ["id", "name", "member_ids"]


class SplitParticipantSerializer(serializers.ModelSerializer):
    friend_name = serializers.CharField(source="friend.name", read_only=True)

    class Meta:
        model = SplitParticipant
        fields = ["id", "friend", "friend_name"]


class SplitShareSerializer(serializers.ModelSerializer):
    friend_name = serializers.CharField(source="friend.name", read_only=True)

    class Meta:
        model = SplitShare
        fields = [
            "id",
            "friend",
            "friend_name",
            "share_amount",
            "percentage",
            "is_settled",
            "settled_at",
        ]


class SplitExpenseSerializer(serializers.ModelSerializer):
    group_name = serializers.CharField(source="group.name", read_only=True)
    paid_by_name = serializers.CharField(source="paid_by.name", read_only=True)
    created_by_name = serializers.CharField(source="created_by.username", read_only=True)
    participants = SplitParticipantSerializer(source="participant_rows", many=True, read_only=True)
    shares = SplitShareSerializer(many=True, read_only=True)

    class Meta:
        model = SplitExpense
        fields = [
            "id",
            "group",
            "group_name",
            "created_by",
            "created_by_name",
            "title",
            "total_amount",
            "paid_by",
            "paid_by_name",
            "split_type",
            "participant_mode",
            "include_payer",
            "created_at",
            "updated_at",
            "participants",
            "shares",
        ]
        read_only_fields = ["id", "created_by", "created_at", "updated_at"]


class GroupDetailSerializer(serializers.ModelSerializer):
    member_rows = GroupMemberSerializer(source="groupmember_set", many=True, read_only=True)
    total_amount = serializers.SerializerMethodField()
    members_count = serializers.SerializerMethodField()

    class Meta:
        model = Group
        fields = [
            "id",
            "name",
            "created_at",
            "member_rows",
            "members_count",
            "total_amount",
        ]

    def get_total_amount(self, obj):
        return obj.split_expenses.aggregate(total=Sum("total_amount"))["total"] or "0.00"

    def get_members_count(self, obj):
        return obj.groupmember_set.count()