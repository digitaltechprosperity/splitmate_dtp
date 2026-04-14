import json
from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.db.models.functions import TruncMonth
from django.db.models import Sum
from .forms import RegisterForm
from .models import Expense


def home(request):
    return render(request, 'home.html')



def register(request):
    if request.method == 'POST':
        form = RegisterForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('login')
    else:
        form = RegisterForm()

    return render(request, 'register.html', {'form': form})


@login_required
def dashboard(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        amount = request.POST.get('amount')
        date = request.POST.get('date')

        is_long_term = request.POST.get('is_long_term') == 'on'
        end_date = request.POST.get('end_date') or None
        interest_rate = request.POST.get('interest_rate') or None

        Expense.objects.create(
            user=request.user,
            name=name,
            amount=amount,
            date=date,
            is_long_term=is_long_term,
            end_date=end_date,
            interest_rate=interest_rate if interest_rate else None
        )

        return redirect('dashboard')

    expenses = Expense.objects.filter(user=request.user).order_by('-date')

    monthly = (
        Expense.objects.filter(user=request.user)
        .annotate(month=TruncMonth('date'))
        .values('month')
        .annotate(total=Sum('amount'))
        .order_by('month')
    )

    labels = []
    data = []

    for m in monthly:
        labels.append(m['month'].strftime('%b %Y'))
        data.append(float(m['total']))

    return render(request, 'dashboard.html', {
        'expenses': expenses,
        'labels': json.dumps(labels),
        'data': json.dumps(data),
    })