"""Bla."""

from collections import OrderedDict

from rest_framework import serializers
from ..base import AsyncWorker, StartJobView
from ..tools import (records_from_data_files, data_to_html_data,
                     file_to_filelike_object,
                     did_you_mean,
                     set_record_topology,
                     matplotlib_figure_to_svg_base64_data)
from ..serializers import FileSerializer

import flametree
from plateo.parsers import plate_from_platemap_spreadsheet
from bandwitch import Clone, BandsObservation, ClonesObservations
from bandwagon import plot_records_digestions


digestion = serializers.ListField(child=serializers.CharField())


class serializer_class(serializers.Serializer):
    constructsMap = FileSerializer(allow_null=True)
    clonesMap = FileSerializer(allow_null=True)
    constructsSequences = serializers.ListField(child=FileSerializer())
    goal = serializers.CharField()
    uniqueDigestion = serializers.BooleanField()
    digestion = serializers.ListField(child=serializers.CharField())
    digestionsMap = FileSerializer(allow_null=True)
    tolerance = serializers.FloatField()
    bandsRange = serializers.ListField(child=serializers.IntegerField())
    fragmentAnalysisArchive = FileSerializer(allow_null=True)
    includeDigestionPlots = serializers.BooleanField()
    ignoreBandsUnder = serializers.IntegerField()
    subanalysis = serializers.CharField()
    topology = serializers.CharField()

def file_type(f):
    return 'csv' if f.name.lower().endswith('csv') else 'excel'

class worker_class(AsyncWorker):

    def work(self):
        self.logger(message="Reading the files...")
        data = self.data

        # PARSE ALL FILES

        constructs_records = records_from_data_files(data.constructsSequences)
        for record in constructs_records:
            set_record_topology(record, data.topology)
        constructs_records = {r.id: r for r in constructs_records}
        constructs_records = OrderedDict(sorted(constructs_records.items()))

        constructs_plate = plate_from_platemap_spreadsheet(
            file_to_filelike_object(data.constructsMap),
            file_type=file_type(data.constructsMap),
            data_field='construct', headers=True)
        constructs_map = OrderedDict([
            (well.name, well.data.construct)
            for well in constructs_plate.iter_wells(direction='row')
            if 'construct' in well.data
            and str(well.data.construct) != 'nan'
        ])
        unknown_constructs = {}
        for well, construct in constructs_map.items():
            if construct not in constructs_records:
                unknown_constructs[construct] = {
                    "well": well,
                    "suggestions": did_you_mean(construct, constructs_records)
                }
        if len(unknown_constructs):
            return {
                "success": False,
                "unknown_constructs": unknown_constructs
            }


        if data.uniqueDigestion:
            digestion = tuple(data.digestion)
            digestions_map = OrderedDict([
                (wellname, digestion)
                for wellname in constructs_map
            ])
        else:
            digestions_plate = plate_from_platemap_spreadsheet(
                file_to_filelike_object(data.digestionsMap),
                file_type=file_type(data.digestionsMap),
                data_field='digestion', headers=True)
            digestions_map = OrderedDict([
                (well.name, tuple(well.data.digestion.split(', ')))
                for well in digestions_plate.iter_wells(direction='row')
                if 'digestion' in well.data
                and str(well.data.digestion) != 'nan'
            ])

        archive = file_to_filelike_object(data.fragmentAnalysisArchive)

        # ANALYZE ALL FILES AND VALIDATE BANDS

        self.logger(message="Analyzing the data...")

        observations = BandsObservation.from_aati_fa_archive(
            archive, ignore_bands_under=data.ignoreBandsUnder)
        clones = Clone.from_bands_observations(observations, constructs_map,
                                               digestions_map)
        clones_observations = ClonesObservations(clones, constructs_records)
        if data.subanalysis == 'partial_digests':
            return self.partial_digests_analysis(data, clones_observations)
        else:
            return self.validation_analysis(data, clones_observations)

    def partial_digests_analysis(self, data, clones_observations):
        analysis = clones_observations.partial_digests_analysis()
        best = max(analysis, key=lambda a: analysis[a]['valid_clones'])
        if analysis[best]['valid_clones'] == analysis[()]['valid_clones']:
            return {
              'message': 'The partial digest analysis did not find any '
                         'significant results',
              'success': 'yeah!'
            }
        ax = clones_observations.plot_partial_digests_analysis(analysis)
        validations = analysis[best]['validations']
        observations = ClonesObservations(
            clones_observations.clones,
            clones_observations.constructs_records,
            partial_cutters=best
        )
        pdf_data = observations.plot_all_validations_patterns(validations)
        return {
            'pdf_file': {
                'data': data_to_html_data(pdf_data, 'pdf'),
                'name': 'digest_validation_assuming_partial_%s.pdf' %
                        "_".join(best),
                'mimetype': 'application/pdf'
            },
            'message': (
                "The analysis shows that the following enzyme(s) were"
                " possibly only partially cutting: %s. <br/>The report below"
                " shows the validation under this hypothesis."
            ) % (", ".join(["<b>%s</b>" % b for b in best])),
            'figure_data':  matplotlib_figure_to_svg_base64_data(
                              ax.figure, bbox_inches='tight'),
            'success': 'yeah!'
        }

    def validation_analysis(self, data, clones_observations):
        validations = clones_observations.validate_all_clones(
            min_band_cutoff=data.bandsRange[0],
            max_band_cutoff=data.bandsRange[1],
            relative_tolerance=data.tolerance
        )
        first_validation = list(validations.values())[0]
        ladder = list(first_validation.clone.digestions.values())[0].ladder

        # CREATE A ZIP WITH VALIDATION REPORTS

        zip_root = flametree.file_tree('@memory')
        self.logger(message="Generating the validation report...")
        zip_root._file('validations.pdf').write(
            clones_observations.plot_all_validations_patterns(validations)
        )
        clones_observations.validations_summary_table(
            validations, zip_root._file('summary.csv').open('w'))
        if data.includeDigestionPlots:
            self.logger(message="Plotting cuts maps...")
            co = clones_observations

            plot_records_digestions(
                target=zip_root._file('digestions.pdf').open('wb'),
                ladder=ladder,
                records_and_digestions=[
                    (co.constructs_records[cst], digestion_)
                    for cst, digestions in co.constructs_digestions.items()
                    for digestion_ in digestions
                ]
            )
            # zip_root._file('digestions.pdf').write(pdf_data)

        self.logger(message="Generating the success plate map...")
        ax = clones_observations.plot_validations_plate_map(validations)
        ax.figure.savefig(zip_root._file('success_map.pdf').open('wb'),
                          format='pdf', bbox_inches='tight')

        self.logger(message="All done !")

        return {
          'zip_file': {
              'data': data_to_html_data(zip_root._close(), 'zip'),
              'name': 'validation_report.zip',
              'mimetype': 'application/zip'
          },
          'success': 'yeah!'
        }

class AnalyzeDigestsView(StartJobView):
    serializer_class = serializer_class
    worker_class = worker_class
