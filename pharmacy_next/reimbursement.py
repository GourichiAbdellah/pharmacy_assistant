"""Estimations ambulatoires par organisme, distinctes des données du catalogue."""
from decimal import Decimal, ROUND_HALF_UP


CNOPS_RULE = {
    "id": "cnops_medicaments_ambulatoires_2026_10_16",
    "organism": "CNOPS",
    "scope": "medicaments_remboursables_soins_ambulatoires",
    "standard_rate": "70",
    "etm_rate": "100",
    "source_url": "https://www.cnops.org.ma/fr/medicaments",
    "checked_on": "2026-10-16",
    "note": "Le complément de mutuelle est distinct. Ne pas appliquer cette règle à la CNSS ou à l'hospitalisation.",
}

CNSS_RULE = {
    "id": "cnss_standard_ambulatoire_2026_10_17",
    "organism": "CNSS",
    "scope": "medicaments_remboursables_soins_ambulatoires_hors_accord_particulier",
    "standard_rate": "70",
    "source_url": "https://www.acaps.ma/fr/grand-public/droits/indemnisation-en-amo",
    "checked_on": "2026-10-17",
    "base_source_column": "cnss_ppv_br_source",
    "note": "PPV-BR est la base CNSS confirmée dans le schéma du projet. Cette estimation standard ne calcule ni ETM individuelle ni hospitalisation.",
}


def estimate_cnss(medication, care_context=None):
    """Estimate one pack under the standard ambulatory assumption; never use PPV fallback."""
    fund = medication["remboursement"]["CNSS"]
    base = fund["base_remboursement"]
    result = {
        "code": medication["code"], "organism": "CNSS", "currency": "MAD",
        "base_remboursement": base, "taux_applique": None,
        "taux_catalogue": fund["taux_pourcent"],
        "montant_rembourse_estime": None, "status": "unavailable",
        "droits_verifies": False, "quantite_boites": 1,
        "hors_complement_mutuelle": True, "rule": dict(CNSS_RULE),
    }
    if fund["remboursable"] is not True:
        result["reason"] = "non_remboursable" if fund["remboursable"] is False else "statut_inconnu"
        return result
    if base is None:
        result["reason"] = "base_cnss_manquante"
        return result
    if care_context in {"hospitalization", "unknown"}:
        result["reason"] = "contexte_de_soins_non_couvert_par_cette_regle"
        return result
    rate = CNSS_RULE["standard_rate"]
    # Preserve conflicting catalogue data instead of silently replacing it with 70.
    if fund["taux_pourcent"] is not None and Decimal(fund["taux_pourcent"]) != Decimal(rate):
        result["reason"] = "taux_catalogue_non_standard"
        result["note"] = "Le catalogue indique un taux différent du standard. Préciser le régime ou l'accord applicable avant tout calcul."
        return result
    amount = (Decimal(base) * Decimal(rate) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    result.update(
        status="estimate", taux_applique=rate,
        montant_rembourse_estime=format(amount, ".2f"), scenario="standard_ambulatoire",
        note="Estimation standard CNSS de 70 % de la base PPV-BR, par boîte. Une ALD/ALC ou un accord ETM peut modifier le taux : les droits et le taux individuel ne sont pas vérifiés.",
    )
    return result


def estimate_cnops(medication, coverage):
    """One presentation, one pack. Coverage declarations are never adjudication."""
    fund = medication["remboursement"]["CNOPS"]
    base = fund["base_remboursement"]
    result = {
        "code": medication["code"], "organism": "CNOPS", "currency": "MAD",
        "base_remboursement": base, "taux_applique": None,
        "montant_rembourse_estime": None, "status": "unavailable",
        "conditions_declarees": dict(coverage), "droits_verifies": False,
        "quantite_boites": 1, "hors_complement_mutuelle": True,
        "rule": dict(CNOPS_RULE),
    }
    if fund["remboursable"] is not True:
        result["reason"] = "non_remboursable" if fund["remboursable"] is False else "statut_inconnu"
        return result
    if base is None:
        result["reason"] = "pbr_manquant"
        return result
    if coverage.get("care_context") in {"hospitalization", "unknown"}:
        result["reason"] = "contexte_de_soins_non_couvert_par_cette_regle"
        return result
    conditions = ("ald_alc_declared", "etm_valid", "treatment_related")
    etm = all(coverage.get(key) is True for key in conditions)
    rate = CNOPS_RULE["etm_rate" if etm else "standard_rate"]
    amount = (Decimal(base) * Decimal(rate) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    result.update(
        status="estimate", taux_applique=rate,
        montant_rembourse_estime=format(amount, ".2f"),
        scenario="etm_selon_declarations" if etm else "standard_ambulatoire",
        conditions_etm_a_preciser=[key for key in conditions if coverage.get(key) is None],
        note=("Estimation à 100 % du PBR selon les déclarations pour ce traitement ; accord et droits non vérifiés."
              if etm else "Estimation au taux standard de 70 % du PBR en soins ambulatoires, hors complément de mutuelle. Ce taux est une hypothèse de calcul, pas une vérification des droits."),
    )
    return result
