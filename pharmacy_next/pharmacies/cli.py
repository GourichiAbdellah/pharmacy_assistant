"""Explicit setup. Never called during web startup."""
import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid5, NAMESPACE_URL

from ..config import Settings
from .models import DirectoryImport, PharmacyError, local_iso, CASABLANCA
from .service import PharmacyService
from .store import PharmacyStore


def demo_document(now):
    pharmacies, duties = [], []
    for i, (name, district, lat, lon) in enumerate([
        ('Pharmacie DEMO Atlas — fictive', 'Centre', 34.0209, -6.8416),
        ('Pharmacie DEMO Jardin — fictive', 'Agdal', 34.0015, -6.8537),
        ('Pharmacie DEMO Rivage — fictive', 'Hassan', 34.025, -6.827),
    ]):
        pid = str(uuid5(NAMESPACE_URL, f'pharmacy-assistant-demo/{i}'))
        pharmacies.append(dict(id=pid, name=name, city='Rabat', district=district,
            address='Adresse fictive pour tester le prototype', latitude=lat, longitude=lon,
            phone=None, partner=i < 2, active=True, is_demo=True))
        pharmacies[-1]['regular_hours'] = ({'open_24h':True} if i==1 else
            {'open_24h':False,'opens_at':'09:00','closes_at':'19:30','days_of_week':list(range(7))})
        if i < 2:
            start=datetime.fromtimestamp(now,CASABLANCA).replace(hour=0,minute=0,second=0,microsecond=0)
            if i==1:start+=timedelta(days=2)
            end=start+timedelta(days=1)
            duties.append(dict(id=str(uuid5(NAMESPACE_URL, f'pharmacy-assistant-demo/duty/{i}')),
                pharmacy_id=pid, starts_at=start.isoformat(), ends_at=end.isoformat(), duty_type='24h',
                source_label='Planning FICTIF de démonstration', source_url=None,
                source_checked_at=local_iso(now), cancelled=False))
    return DirectoryImport.model_validate(dict(pharmacies=pharmacies, duties=duties))


def main():
    parser = argparse.ArgumentParser(description='Administration locale du module pharmacies')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Crée explicitement les tables assist_* manquantes')
    commands.add_parser('demo', help='Initialise 3 pharmacies fictives et renouvelle leurs accès (SQLite + mode demo seulement)')
    commands.add_parser('refresh-demo', help='Actualise les horaires fictifs en conservant les accès pharmacien existants')
    imp = commands.add_parser('import', help='Importe un annuaire/planning JSON validé, sans effacer les lignes absentes')
    imp.add_argument('file', type=Path)
    access = commands.add_parser('issue-access', help='Crée/renouvelle un accès pharmacien valable 30 jours')
    access.add_argument('pharmacy_id', type=UUID)
    link = commands.add_parser('link-google', help='Lie un Place ID après vérification manuelle de l’identité de la pharmacie')
    link.add_argument('pharmacy_id', type=UUID)
    link.add_argument('place_id')
    unlink = commands.add_parser('unlink-google', help='Supprime le lien Google d’une pharmacie')
    unlink.add_argument('pharmacy_id', type=UUID)
    probe = commands.add_parser('probe-duty', help='Diagnostic Le Matin uniquement : ne publie ni importe aucune garde')
    probe.add_argument('--date', required=True, help='YYYY-MM-DD')
    probe.add_argument('--garde', choices=['jour', 'nuit', '24'], required=True)
    args = parser.parse_args()
    settings = Settings.from_env()
    store = PharmacyStore(settings)
    service = PharmacyService(store, None)
    try:
        if args.command == 'init':
            store.initialize()
            print('Tables assist_* initialisées. Les tables médicaments/notices ne sont pas modifiées.')
        elif args.command in {'demo','refresh-demo'}:
            if settings.pharmacy_backend != 'sqlite' or not service.is_demo:
                parser.error('demo nécessite PHARMACY_BACKEND=sqlite et PHARMACY_ENVIRONMENT=demo')
            store.initialize()
            with store.transaction(write=True) as tx:
                tx.execute("UPDATE assist_duties SET cancelled=1 WHERE pharmacy_id IN (SELECT id FROM assist_pharmacies WHERE is_demo=1) AND source_label='Planning FICTIF de démonstration'")
            document = demo_document(store.now())
            print(json.dumps(service.import_directory(document), ensure_ascii=False))
            if args.command=='refresh-demo':
                print('Horaires FICTIFS actualisés. Les accès et demandes existants sont conservés. Aucun destinataire réel.')
                return
            print('PHARMACIES FICTIVES. Aucun destinataire réel. Les anciens accès de ces pharmacies sont révoqués.')
            for pharmacy in document.pharmacies:
                if pharmacy.partner:
                    result = store.issue_access('pharmacist', str(pharmacy.id))
                    print(f'{pharmacy.name}\nIdentifiant : {pharmacy.id}\nCode privé : {result["token"]}\nExpire : {local_iso(result["expires_at"])}\n')
            print('Ouvre /pharmacien et saisis un code privé. Recherche Rabat côté patient.')
        elif args.command == 'import':
            document = DirectoryImport.model_validate_json(args.file.read_text(encoding='utf-8-sig'))
            print(json.dumps(service.import_directory(document), ensure_ascii=False))
        elif args.command == 'link-google':
            store.link_google(args.pharmacy_id, args.place_id)
            print('Lien vérifié enregistré. Cela ne crée pas un compte partenaire ni un accès pharmacien.')
        elif args.command == 'unlink-google':
            store.unlink_google(args.pharmacy_id)
            print('Lien Google supprimé.')
        elif args.command == 'probe-duty':
            from .duty_probe import probe_lematin
            print(json.dumps(probe_lematin(settings, args.date, args.garde), ensure_ascii=False, indent=2))
        else:
            result = store.issue_access('pharmacist', str(args.pharmacy_id))
            print('Ancien accès révoqué. Conserve ce code privé hors du dépôt :')
            print(result['token'])
            print('Expiration :', local_iso(result['expires_at']))
    except PharmacyError as exc:
        parser.exit(1, exc.message+'\n')
    except (ValueError, OSError):
        parser.exit(1, 'Fichier invalide/inaccessible. Vérifie le schéma dans docs/PHARMACIES.md.\n')


if __name__ == '__main__':
    main()
