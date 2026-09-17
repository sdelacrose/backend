from django.db.models import (
    Case,
    F,
    Func,
    IntegerField,
    OrderBy,
    TextField,
    Value,
    When,
)
from django.db.models.functions import Lower, NullIf, Trim
from rest_framework.filters import OrderingFilter


class NaturalSortKey(Func):
    output_field = TextField()

    def as_sql(self, compiler, connection, **extra_context):
        expression_sql, params = compiler.compile(self.source_expressions[0])
        return (
            "("
            "SELECT STRING_AGG("
            "CASE WHEN chunk[1] ~ '^[0-9]+$' "
            "THEN LPAD(chunk[1], 100, '0') "
            "ELSE chunk[1] END, "
            "'' ORDER BY position"
            ") "
            f"FROM REGEXP_MATCHES({expression_sql}, "
            "'([0-9]+|[^0-9]+)', 'g') "
            "WITH ORDINALITY AS parts(chunk, position)"
            ")",
            params,
        )


class TableOrderingFilter(OrderingFilter):
    @staticmethod
    def _localized_value_order(request, view, field_name):
        localized_fields = set(
            getattr(view, "ordering_localized_fields", ())
        )
        if field_name not in localized_fields:
            return None

        raw_values = request.query_params.get(f"{field_name}_order", "")
        values = []
        seen = set()
        for value in raw_values.split(","):
            value = value.strip()
            if value and value not in seen:
                values.append(value)
                seen.add(value)
        return values[:500]

    def filter_queryset(self, request, queryset, view):
        ordering = self.get_ordering(request, queryset, view)
        if not ordering:
            return queryset

        text_fields = set(getattr(view, "ordering_text_fields", ()))
        natural_fields = set(getattr(view, "ordering_natural_fields", ()))
        localized_array_fields = set(
            getattr(view, "ordering_localized_array_fields", ())
        )
        field_aliases = dict(getattr(view, "ordering_field_aliases", {}))
        order_by = []
        ordered_names = set()

        for index, field in enumerate(ordering):
            descending = field.startswith("-")
            name = field[1:] if descending else field
            expression_name = field_aliases.get(name, name)
            ordered_names.add(name)

            localized_value_order = self._localized_value_order(
                request,
                view,
                name,
            )
            if localized_value_order is not None:
                if not localized_value_order:
                    continue
                localized_alias = f"_ordering_localized_{index}"
                is_localized_array = name in localized_array_fields
                localized_lookup = (
                    f"{expression_name}__contains"
                    if is_localized_array
                    else expression_name
                )
                queryset = queryset.alias(
                    **{
                        localized_alias: Case(
                            *[
                                When(
                                    **{
                                        localized_lookup: (
                                            [value]
                                            if is_localized_array
                                            else value
                                        ),
                                        "then": Value(position),
                                    }
                                )
                                for position, value in enumerate(
                                    localized_value_order
                                )
                            ],
                            default=Value(None),
                            output_field=IntegerField(),
                        )
                    }
                )
                order_by.append(
                    OrderBy(
                        F(localized_alias),
                        descending=descending,
                        nulls_last=True,
                    )
                )
                continue

            if name not in text_fields and name not in natural_fields:
                order_by.append(
                    OrderBy(
                        F(expression_name),
                        descending=descending,
                        nulls_last=True,
                    )
                )
                continue

            text_alias = f"_ordering_text_{index}"
            queryset = queryset.alias(
                **{
                    text_alias: Lower(
                        NullIf(
                            NullIf(
                                Trim(F(expression_name)),
                                Value(""),
                                output_field=TextField(),
                            ),
                            Value("-"),
                            output_field=TextField(),
                        )
                    )
                }
            )

            order_by.append(
                Case(
                    When(**{f"{text_alias}__isnull": True}, then=Value(1)),
                    default=Value(0),
                    output_field=IntegerField(),
                )
            )

            if name in natural_fields:
                natural_alias = f"_ordering_natural_{index}"
                queryset = queryset.alias(
                    **{natural_alias: NaturalSortKey(F(text_alias))}
                )
                order_by.append(
                    OrderBy(
                        F(natural_alias),
                        descending=descending,
                        nulls_last=True,
                    )
                )

            order_by.append(OrderBy(F(text_alias), descending=descending))

        if "id" not in ordered_names:
            order_by.append("id")
        return queryset.order_by(*order_by)
