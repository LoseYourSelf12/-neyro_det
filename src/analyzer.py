import logging

def average_counts(counts_list):
    """
    Получить среднее число машин по серии кадров.
    """
    if not counts_list:
        return 0
    return sum(counts_list) / len(counts_list)