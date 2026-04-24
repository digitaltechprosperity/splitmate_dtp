from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.db import models

from .models import Expense, Friend, Group, SplitExpense

from django import forms
from django.contrib.auth.models import User


from django import forms
from django.contrib.auth.models import User


class RegisterForm(forms.ModelForm):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={
            "class": "form-control",
            "placeholder": "Enter Gmail address"
        })
    )

    class Meta:
        model = User
        fields = ["username", "email"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["username"].widget.attrs.update({
            "class": "form-control",
            "placeholder": "Enter username"
        })

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()

        if not email.endswith("@gmail.com"):
            raise forms.ValidationError("Please enter a valid Gmail address.")

        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this Gmail already exists.")

        return email
    
class FriendForm(forms.ModelForm):
    class Meta:
        model = Friend
        fields = ["name", "email"]
        widgets = {
            "name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Enter friend name"
            }),
            "email": forms.EmailInput(attrs={
                "class": "form-control",
                "placeholder": "Enter email address"
            }),
        }

    def __init__(self, *args, **kwargs):
        self.current_user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = self.cleaned_data.get("name")
        if not name:
            raise forms.ValidationError("Please enter name.")
        return name.strip()

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if not email:
            raise forms.ValidationError("Please enter email address.")
        email = email.strip().lower()
        if self.current_user:
            qs = Friend.objects.filter(user=self.current_user, email=email)
            if self.instance and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError("You already have a friend with this email address.")
        return email


class GroupForm(forms.ModelForm):
    members = forms.ModelMultipleChoiceField(
        queryset=Friend.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        required=False
    )

    class Meta:
        model = Group
        fields = ["name", "members"]
        widgets = {
            "name": forms.TextInput(attrs={
                "class": "form-control",
                "placeholder": "Enter group name"
            }),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        if user:
            qs = Friend.objects.filter(user=user).order_by("name")
            self.fields["members"].queryset = qs


class ExpenseForm(forms.ModelForm):
    class Meta:
        model = Expense
        fields = ["name", "amount", "date", "is_long_term", "end_date", "interest_rate"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "amount": forms.NumberInput(attrs={"class": "form-control"}),
            "date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "end_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "interest_rate": forms.NumberInput(attrs={"class": "form-control"}),
        }


class SplitExpenseForm(forms.ModelForm):
    selected_participants = forms.ModelMultipleChoiceField(
        queryset=Friend.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        required=False
    )

    custom_split_data = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={"id": "custom-split-data"})
    )

    percentage_split_data = forms.CharField(
        required=False,
        widget=forms.HiddenInput(attrs={"id": "percentage-split-data"})
    )

    paid_by = forms.ModelChoiceField(
        queryset=Friend.objects.none(),
        required=False,
        widget=forms.HiddenInput()
    )

    class Meta:
        model = SplitExpense
        fields = [
            "group",
            "title",
            "total_amount",
            "paid_by",
            "split_type",
            "participant_mode",
            "selected_participants",
            "include_payer",
        ]
        widgets = {
            "group": forms.Select(attrs={"class": "form-control", "id": "group-select"}),
            "title": forms.TextInput(attrs={"class": "form-control", "placeholder": "Enter expense title"}),
            "total_amount": forms.NumberInput(attrs={"class": "form-control", "placeholder": "Enter total amount"}),
            "split_type": forms.Select(attrs={"class": "form-control", "id": "split-type-select"}),
            "participant_mode": forms.Select(attrs={"class": "form-control", "id": "participant-mode-select"}),
            "include_payer": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        group = kwargs.pop("group", None)
        super().__init__(*args, **kwargs)

        self.fields["group"].queryset = Group.objects.none()
        self.fields["paid_by"].queryset = Friend.objects.none()
        self.fields["selected_participants"].queryset = Friend.objects.none()

        if user:
            groups = Group.objects.filter(
                models.Q(user=user) | models.Q(members__linked_user=user)
            ).distinct().order_by("-created_at")
            self.fields["group"].queryset = groups

        target_group = group

        if not target_group and self.instance and self.instance.pk:
            target_group = self.instance.group

        if not target_group and self.is_bound:
            group_id = self.data.get("group")
            if group_id:
                target_group = Group.objects.filter(id=group_id).first()

        if target_group:
            members = target_group.all_group_friends().order_by("name")
            self.fields["paid_by"].queryset = members
            self.fields["selected_participants"].queryset = members

            if self.instance and self.instance.pk:
                self.fields["selected_participants"].initial = self.instance.get_selected_participants()

        if user:
            friend = Friend.objects.filter(linked_user=user).first()
            if friend:
                self.fields["paid_by"].initial = friend

    def clean_total_amount(self):
        amount = self.cleaned_data.get("total_amount")
        if amount is None or amount <= 0:
            raise forms.ValidationError("Total amount must be greater than zero.")
        return amount

    def clean(self):
        cleaned_data = super().clean()

        participant_mode = cleaned_data.get("participant_mode")
        selected_participants = cleaned_data.get("selected_participants")
        split_type = cleaned_data.get("split_type")

        if participant_mode == SplitExpense.PARTICIPANT_MODE_SELECTED and not selected_participants:
            raise forms.ValidationError("Please choose at least one selected participant.")

        if split_type == SplitExpense.SPLIT_TYPE_CUSTOM and not cleaned_data.get("custom_split_data"):
            raise forms.ValidationError("Custom split data is required.")

        if split_type == SplitExpense.SPLIT_TYPE_PERCENTAGE and not cleaned_data.get("percentage_split_data"):
            raise forms.ValidationError("Percentage split data is required.")

        return cleaned_data


from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User


class EmailOrUsernameLoginForm(AuthenticationForm):
    username = forms.CharField(
        widget=forms.TextInput(attrs={
            "class": "form-control",
            "placeholder": "Enter username or Gmail",
            "autofocus": True,
        })
    )

    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            "class": "form-control",
            "placeholder": "Enter password",
        })
    )

    def clean(self):
        username_or_email = self.cleaned_data.get("username")
        password = self.cleaned_data.get("password")

        if username_or_email and password:
            username_or_email = username_or_email.strip()

            user_obj = User.objects.filter(email__iexact=username_or_email).first()

            if user_obj:
                username = user_obj.username
            else:
                username = username_or_email

            self.user_cache = authenticate(
                self.request,
                username=username,
                password=password
            )

            if self.user_cache is None:
                raise forms.ValidationError("Invalid username/Gmail or password.")

        return self.cleaned_data