import json
from typing import Literal
from urllib.parse import quote_plus

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from .catalogue import notice_compatibility
from .data import DataUnavailable, NOTICE_COLUMNS, SECTIONS, text
from .reimbursement import estimate_cnops, estimate_cnss
from .pharmacies.models import PharmacyError, NearbyInput, DraftInput


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Search(Arguments):
    queries: list[str] = Field(min_length=1, max_length=3, description="Nom brut puis orthographes ou translittérations possibles.")
    limit: int = Field(default=20, ge=1, le=30)
    offset: int = Field(default=0, ge=0, le=20000)


class GetMedications(Arguments):
    codes: list[str] = Field(min_length=1, max_length=20)


class ReadNotice(Arguments):
    notice_id: int = Field(ge=1)
    sections: list[Literal[*SECTIONS]] = Field(min_length=1, max_length=3)
    medication_code: str | None = Field(default=None, description="Code de la présentation concernée si connu.")


class Remember(Arguments):
    selected_codes: list[str] | None = Field(default=None, max_length=20, description="Codes réellement précisés par la personne. [] efface la sélection.")
    organism: Literal["CNSS", "CNOPS"] | None = None


class Empty(Arguments):
    pass


class SearchPharmacies(Arguments):
    city: str | None = Field(default=None, max_length=100)
    district: str | None = Field(default=None, max_length=100)
    on_duty_only: bool = False
    open_confirmed_only: bool = False
    radius_km: float = Field(default=10, gt=0, le=100)


class NearestRequest(Arguments):
    medication_code: str | None = Field(default=None,max_length=50)
    city: str | None = Field(default=None,max_length=100)
    district: str | None = Field(default=None,max_length=100)
    radius_km: float = Field(default=10,gt=0,le=100)
    boxes: int = Field(default=1,ge=1,le=10,strict=True)


PHARMACY_TOOLS = {'search_pharmacies', 'prepare_availability_request', 'prepare_nearest_availability_request','get_availability_requests'}


class EstimateCnops(Arguments):
    medication_code: str = Field(min_length=1, max_length=50)
    ald_alc_declared: StrictBool | None = Field(default=None, description="ALD/ALC déclarée à la CNOPS selon l'utilisateur. Ne pas déduire du médicament.")
    etm_valid: StrictBool | None = Field(default=None, description="Accord ETM obtenu et valide selon l'utilisateur ; maladie chronique seule insuffisante.")
    treatment_related: StrictBool | None = Field(default=None, description="Ce médicament est prescrit pour l'affection couverte par cet accord selon l'utilisateur.")
    care_context: Literal["ambulatory", "hospitalization", "unknown"] | None = Field(default=None, description="Contexte déclaré. Par défaut, estimation sous hypothèse ambulatoire. Hospitalisation exclue de cette règle.")
    reset_context: bool = Field(default=False, description="Effacer les déclarations de ce code lors d'un changement de patient ou de traitement.")


class EstimateCnss(Arguments):
    medication_code: str = Field(min_length=1, max_length=50)
    care_context: Literal["ambulatory", "hospitalization", "unknown"] | None = Field(
        default=None, description="Contexte déclaré ; par défaut hypothèse ambulatoire. Ne calcule ni hospitalisation ni accord ETM individuel.")


DEFINITIONS = {
    "search_medications": (Search, "Cherche le nom/code/DCI dans CNOPS/CNSS et renvoie les présentations, prix et remboursements. Plusieurs résultats peuvent être comparés directement."),
    "get_medication_details": (GetMedications, "Relit les données financières des codes exacts. Aucun choix manuel préalable obligatoire."),
    "estimate_cnops_reimbursement": (EstimateCnops, "Calcule en Python une estimation CNOPS par boîte : 70 % du PBR par défaut ambulatoire ; 100 % seulement si ALD/ALC déclarée, ETM valide et traitement lié sont tous déclarés. Omettre les champs inconnus ; null efface une déclaration. Ne calcule pas la CNSS."),
    "estimate_cnss_reimbursement": (EstimateCnss, "Calcule en Python l'estimation standard CNSS : 70 % de la base cnss_ppv_br_source (PPV-BR), par boîte. Jamais sur le prix public ou le PBR CNOPS. Ne calcule pas les taux d'accords ALD/ETM individuels."),
    "search_notices": (Search, "Recherche indépendante des métadonnées Asafar. Ne suppose aucun lien avec le catalogue financier."),
    "read_notice": (ReadNotice, "Lit les rubriques demandées d'une fiche Asafar déjà trouvée. Vérifie la compatibilité avec la présentation si connue."),
    "remember_context": (Remember, "Retient la présentation et/ou l'organisme précisés par l'utilisateur pour les prochains tours."),
    "find_nearby_pharmacy": (Empty, "Retourne un lien Google Maps uniquement sur demande de pharmacie ou après accord explicite de la personne."),
    "search_pharmacies": (SearchPharmacies, "Recherche les pharmacies proches via le fournisseur configuré et l'annuaire des partenaires. Les gardes viennent exclusivement des plannings datés vérifiés. Utilise la position navigateur, sinon une ville précisée. Aucun stock déduit."),
    "prepare_availability_request": (DraftInput, "Prépare un brouillon pour une présentation exacte déjà trouvée et une pharmacie partenaire. NE transmet PAS la demande : le patient doit confirmer le récapitulatif dans l'interface."),
    "prepare_nearest_availability_request": (NearestRequest, "Prépare automatiquement la demande de disponibilité auprès du partenaire admissible le plus proche de la position navigateur. Aucun formulaire à remplir. Sans GPS, préciser ville/quartier ; ne jamais inventer une distance. Réutilise la présentation précisée dans la conversation ou demande une clarification naturelle. Une confirmation d’envoi dans le chat reste nécessaire."),
    "get_availability_requests": (Empty, "Relit les demandes et réponses horodatées du patient. Une réponse disponible n'est pas une réservation."),
}
TOOL_SCHEMAS = [{"type": "function", "function": {
    "name": name, "description": description, "parameters": model.model_json_schema(),
}} for name, (model, description) in DEFINITIONS.items()]


class ToolRunner:
    def __init__(self, catalogue, context, *, selection_requires_confirmation=False,
                 pharmacies=None, patient=None, location=None):
        self.catalogue, self.context = catalogue, context
        self.selection_requires_confirmation = selection_requires_confirmation
        self.sources = {}
        self.errors = []
        self.pharmacies, self.patient, self.location = pharmacies, patient, location
        self.ui_actions = []
        self.prepared_request = None
        self.schemas = [s for s in TOOL_SCHEMAS if pharmacies or s['function']['name'] not in PHARMACY_TOOLS]

    def _collect_medications(self, rows):
        self.context.remember_records(rows)
        for row in rows:
            self.sources[f"med:{row['code']}"] = {
                "type": "catalogue", "id": row["code"], "label": row["nom"],
                "presentation": row["presentation"], "source": "CNOPS/CNSS",
            }

    def execute(self, name, raw_arguments):
        if name not in DEFINITIONS or (name in PHARMACY_TOOLS and not self.pharmacies):
            return {"error": "unknown_tool", "message": "Outil inconnu."}
        try:
            args = DEFINITIONS[name][0].model_validate_json(raw_arguments)
            if isinstance(args, Search) and any(not q.strip() or len(q) > 160 for q in args.queries):
                return {"error": "invalid_arguments", "message": "Chaque recherche doit contenir un nom ou une DCI de 1 à 160 caractères."}
        except (ValidationError, ValueError, TypeError):
            return {"error": "invalid_arguments", "message": "Paramètres invalides. Respecte le schéma de l'outil."}
        try:
            return self._execute(name, args)
        except PharmacyError as exc:
            self.errors.append(name)
            return {'error': 'pharmacy_action_unavailable', 'message': exc.message}
        except DataUnavailable as exc:
            self.errors.append(name)
            return {"error": "data_unavailable", "message": str(exc),
                    "instruction": "Explique cette indisponibilité. N'invente aucune donnée."}
        except Exception:
            self.errors.append(name)
            return {"error": "tool_failed", "message": "La lecture des données a échoué temporairement."}

    def _execute(self, name, args):
        if name in {'prepare_nearest_availability_request','prepare_availability_request'} and self.prepared_request:
            return {**self.prepared_request,'instruction':'Le récapitulatif est déjà affiché dans le chat. Ne prépare pas une deuxième demande dans cette réponse. Le patient doit confirmer le premier récapitulatif.'}
        if name=='prepare_nearest_availability_request':
            if not self.patient:
                return {'error':'patient_access_needed','message':'Le suivi des demandes doit être activé dans l’interface.'}
            if self.selection_requires_confirmation:
                return {'error':'photo_needs_confirmation','message':'Clarifie la lecture de la photo dans la discussion avant de préparer la demande.'}
            code=args.medication_code or (self.context.selected_codes[0] if len(self.context.selected_codes)==1 else None)
            if not code or code not in self.context.known_medications:
                return {'error':'presentation_needed','message':'Retrouve le médicament dans le catalogue. Si plusieurs présentations restent possibles, demande une précision simple dans la conversation. Ne renvoie pas vers un formulaire.'}
            result=self.pharmacies.prepare_nearest(self.patient,code,args.boxes,location=self.location,
                city=args.city,district=args.district,radius_km=args.radius_km)
            if result.get('error')=='location_needed':
                self.ui_actions.append({'type':'location_request'})
            elif not result.get('error'):
                self.prepared_request=result['request']
                self.ui_actions.append({'type':'draft','data':result['request']})
                result['instruction']='La demande est préparée, pas envoyée. Explique le partenaire choisi et affiche le récapitulatif déjà dans le chat. Le patient n’a aucun formulaire à remplir, seulement le bouton Confirmer et envoyer.'
            return result
        if name == 'search_pharmacies':
            values = args.model_dump()
            if self.location:
                values.update(self.location)
            if not values.get('city') and not self.location:
                return {'error': 'location_needed', 'message': 'Demande la ville ou propose de partager la position via le bouton Pharmacies.'}
            result = self.pharmacies.search(NearbyInput(**values))
            self.ui_actions.append({'type': 'pharmacies', 'data': result})
            return self.pharmacies.conversation_result(result)
        if name in {'prepare_availability_request', 'get_availability_requests'}:
            if not self.patient:
                return {'error': 'patient_access_needed', 'message': 'Ouvre le panneau Pharmacies pour activer le suivi de tes demandes.'}
            if name == 'get_availability_requests':
                rows = self.pharmacies.list_requests(self.patient)
                self.ui_actions.append({'type': 'requests', 'data': rows})
                return {'requests': rows}
            if self.selection_requires_confirmation:
                return {'error': 'photo_needs_confirmation', 'message': 'Clarifie la lecture de la photo avant de préparer une demande.'}
            if args.medication_code not in self.context.known_medications:
                return {'error': 'medication_not_retrieved', 'message': 'Recherche cette présentation dans le catalogue avant de préparer le récapitulatif.'}
            result = self.pharmacies.draft(self.patient, args)
            self.prepared_request=result
            self.ui_actions.append({'type': 'draft', 'data': result})
            return {**result, 'instruction': 'Affiche le récapitulatif. Le bouton de confirmation du patient est indispensable. Ne dis jamais que ce brouillon a été envoyé.'}
        if name == "search_medications":
            result = self.catalogue.search_medications(args.queries, args.limit, args.offset)
            self._collect_medications(result["results"])
            result["note"] = "Candidats issus de la base. Les ressemblances de nom ne confirment pas l'identité. Les variantes peuvent être comparées dès maintenant."
            return result
        if name == "get_medication_details":
            result = self.catalogue.get_medications(args.codes)
            self._collect_medications(result["results"])
            return result
        if name == "estimate_cnops_reimbursement":
            code = args.medication_code
            if code not in self.context.known_medications:
                return {"error": "medication_not_retrieved", "message": "Recherche d'abord cette présentation dans le catalogue."}
            records = self.catalogue.get_medications([code])["results"]
            if not records:
                return {"error": "medication_not_found"}
            self._collect_medications(records)
            coverage = {} if args.reset_context else dict(self.context.cnops_coverage_by_code.get(code, {}))
            for field in ("ald_alc_declared", "etm_valid", "treatment_related", "care_context"):
                if field in args.model_fields_set:
                    coverage[field] = getattr(args, field)
            result = estimate_cnops(records[0], coverage)
            self.context.cnops_coverage_by_code[code] = coverage
            self.context.cnops_estimates[code] = result
            self.sources["rule:cnops"] = {
                "type": "coverage_rule", "id": result["rule"]["id"],
                "label": "Règle CNOPS médicaments ambulatoires", "source": "CNOPS",
                "url": result["rule"]["source_url"],
            }
            return result
        if name == "estimate_cnss_reimbursement":
            code = args.medication_code
            if code not in self.context.known_medications:
                return {"error": "medication_not_retrieved", "message": "Recherche d'abord cette présentation dans le catalogue."}
            records = self.catalogue.get_medications([code])["results"]
            if not records:
                return {"error": "medication_not_found"}
            self._collect_medications(records)
            result = estimate_cnss(records[0], args.care_context)
            self.context.cnss_estimates[code] = result
            self.sources["rule:cnss"] = {
                "type": "coverage_rule", "id": result["rule"]["id"],
                "label": "Taux standard CNSS et base PPV-BR", "source": "ACAPS / CNSS",
                "url": result["rule"]["source_url"],
            }
            return result
        if name == "search_notices":
            result = self.catalogue.search_notices(args.queries, min(args.limit, 15), args.offset)
            ids = [str(r["id"]) for r in result["results"]]
            self.context.recent_notice_ids = list(dict.fromkeys(self.context.recent_notice_ids + ids))[-60:]
            result["linkage"] = "independent_source_no_automatic_join"
            return result
        if name == "read_notice":
            if str(args.notice_id) not in self.context.recent_notice_ids:
                return {"error": "notice_not_retrieved", "message": "Recherche d'abord cette notice avec search_notices."}
            row = self.catalogue.repository.notice(args.notice_id)
            if row is None:
                return {"error": "notice_not_found"}
            code = args.medication_code
            if code is None and len(self.context.selected_codes) == 1:
                code = self.context.selected_codes[0]
            result = {"metadata": {k: text(row.get(k)) for k in NOTICE_COLUMNS},
                      "linkage": {"status": "not_evaluated"}}
            if code:
                if code not in self.context.known_medications:
                    return {"error": "medication_not_retrieved", "message": "Lis d'abord les données de ce code."}
                result["linkage"] = notice_compatibility(self.context.known_medications[code], row)
                if result["linkage"]["status"] != "compatible_fields":
                    result["sections"] = {}
                    result["message"] = "Cette fiche n'est pas validée pour cette présentation. Aucun contenu clinique attribué."
                    return result
            result["sections"] = {}
            for section in args.sections:
                raw = text(row.get(section))
                available = bool(raw and raw.casefold() not in {"aucune info", "na", "n/a", "?"})
                result["sections"][section] = {
                    "available": available,
                    "text": raw[:12000] if available else "aucune info",
                    "truncated": bool(available and len(raw) > 12000),
                }
            result["note"] = "Aucune info = information indisponible, pas absence de risque. Un texte tronqué n'est pas une notice complète."
            self.sources[f"notice:{args.notice_id}"] = {
                "type": "notice", "id": str(args.notice_id), "label": text(row.get("titre")),
                "source": "Asafar", "url": text(row.get("url_source")),
            }
            return result
        if name == "remember_context":
            if args.selected_codes and self.selection_requires_confirmation:
                return {"error": "photo_needs_confirmation", "message": "Lecture photo incertaine : présente les candidats comme des possibilités et demande une précision écrite ou une nouvelle photo avant de mémoriser une présentation."}
            if args.selected_codes is not None:
                if any(c not in self.context.known_medications for c in args.selected_codes):
                    return {"error": "unknown_medication_code", "message": "Ces codes n'ont pas été trouvés dans le catalogue au cours de la conversation."}
                self.context.selected_codes = list(dict.fromkeys(args.selected_codes))
            if "organism" in args.model_fields_set:
                self.context.organism = args.organism
            return {"selected_codes": self.context.selected_codes, "organism": self.context.organism}
        if name == "find_nearby_pharmacy":
            return {"url": "https://www.google.com/maps/search/?api=1&query=" + quote_plus("pharmacie à proximité"),
                    "note": "La localisation dépend de Google Maps. Aucun stock vérifié."}


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=str)
