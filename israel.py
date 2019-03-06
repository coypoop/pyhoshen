# -*- coding: utf-8 -*-
"""
Supporting analysis functions of Israeli Election results.
"""

from . import models
from . import utils
import theano
import theano.tensor as T
from theano.ifelse import ifelse
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
import datetime

def get_version():
    return 16

class IsraeliElectionForecastModel(models.ElectionForecastModel):
    """
    A class that encapsulates computations specific to the Israeli Election
    such as Bader-Ofer Knesset seat computations.
    """
    def __init__(self, config, *args, **kwargs):
        super(IsraeliElectionForecastModel, self).__init__(config, *args, **kwargs)

    def day_index(self, d):
        """
        Computes the days before the forecast date of a given date.
        
        Forecast day is considered day-index 0, and the day index increases
        for each day beforehand.
        """
        return (self.forecast_model.forecast_day - datetime.datetime.strptime(d, '%d/%m/%Y')).days
    
    def create_surplus_matrices(self):
        """
        Create matrices that represent the surplus agreements between
        political parties, used during the Bader-Ofer computations.
        """
        fe = self.forecast_model

        num_parties = len(fe.parties)
        surplus_matrices = np.stack([ np.eye(num_parties, dtype="int64") ] * fe.num_days)
        for day in range(fe.num_days):
            cur_agreements = [ sa for sa in fe.config['surplus_agreements']
                if 'since' not in sa or self.day_index(sa['since']) >= day ]
            for agreement in cur_agreements:
              party1 = fe.party_ids.index(agreement['name1'])
              party2 = fe.party_ids.index(agreement['name2'])
              surplus_matrices[day, party1, party2] = 1
              surplus_matrices[day, party2, party2] = 0
            
        return surplus_matrices
    
    def compute_trace_bader_ofer(self, trace, surpluses = None, threshold = None):
        """
        Compute the Bader-Ofer on a full sample trace using theano scan.
        
        Example usage:
            bo=election.compute_trace_bader_ofer(samples['support'])
            
        trace should be of dimensions nsamples x ndays x nparties
        """
        # trace : nsamples x ndays x nparties
        num_seats = T.constant(120)
    
        def bader_ofer_fn___(prior, votes):
            moded = votes / (prior + 1)
            return prior + T.eq(moded, moded.max())
        
        def bader_ofer_fn__(cur_seats, prior, votes):
            new_seats = ifelse(T.lt(cur_seats, num_seats), bader_ofer_fn___(prior, votes), prior)
            return (cur_seats + 1, new_seats.astype("int64")), theano.scan_module.until(T.ge(cur_seats, num_seats))
        
        # iterate a particular day of a sample, and compute the bader-ofer allocation
        def bader_ofer_fn_(seats, votes, surplus_matrix):
          initial_seats = surplus_matrix.dot(seats)
          comp_ejs__, upd_ejs__ = theano.scan(fn = bader_ofer_fn__,
            outputs_info = [initial_seats.sum(), initial_seats], non_sequences = [surplus_matrix.dot(votes)], n_steps = num_seats)
          joint_seats = comp_ejs__[1][-1]
          surplus_t = surplus_matrix.T
          has_seats_t = surplus_t * T.gt(surplus_t.sum(0),1)
          is_joint_t = T.gt(surplus_t.sum(0),1).dot(surplus_matrix)
          non_joint = T.eq(is_joint_t, 0)
          votes_t = votes.dimshuffle(0, 'x')
          our_votes_t = surplus_t * votes_t
          joint_moded = T.switch(T.eq(joint_seats, 0), 0, our_votes_t.sum(0) / joint_seats)
          joint_moded_both_t = joint_moded * has_seats_t
          initial_seats_t = T.switch(T.eq(joint_moded_both_t, 0), 0, our_votes_t // joint_moded_both_t)
          moded_t = T.switch(T.eq(joint_moded_both_t, 0), 0, votes_t / (initial_seats_t + 1))
          added_seats = T.eq(moded_t, moded_t.max(0)) * has_seats_t
          joint_added = initial_seats_t.sum(1) + added_seats.sum(1).astype("int64")
          return joint_seats * non_joint + joint_added * is_joint_t
        
        # iterate each day of a sample, and compute for each the bader-ofer allocation
        def bader_ofer_fn(seats, votes, surplus_matrices):
          comp_bo_, _ = theano.scan(fn = bader_ofer_fn_, sequences=[seats, votes, surplus_matrices])
          return comp_bo_
        
        if threshold is None:
            threshold = float(self.forecast_model.config['threshold_percent']) / 100

        if surpluses is None:
            surpluses = self.create_surplus_matrices()
            
        votes = T.tensor3("votes")
        seats = T.tensor3("seats", dtype='int64')
        surplus_matrices = T.tensor3("surplus_matrices", dtype='int64')
        
        # iterate each sample, and compute for each the bader-ofer allocation
        comp_bo, _ = theano.scan(bader_ofer_fn, sequences=[seats, votes], non_sequences=[surplus_matrices])
        compute_bader_ofer = theano.function(inputs=[seats, votes, surplus_matrices], outputs=comp_bo)
        
        kosher_votes = trace.sum(axis=2,keepdims=True)
        
        passed_votes = trace / kosher_votes
        passed_votes[passed_votes < threshold] = 0
        
        initial_moded = (kosher_votes / 120)
        initial_seats = (passed_votes // initial_moded).astype('int64')
    
        ndays = trace.shape[1]
        nparties = trace.shape[2]
        
        if surpluses is None:
            surpluses = self.create_surplus_matrices()

        return compute_bader_ofer(initial_seats, passed_votes,
            surpluses * np.ones([ndays, nparties, nparties], dtype='int64'))
        
    def get_least_square_sum_seats(self, bader_ofer, day=0):
        """
        Determine the sample whose average distance in seats to the other samples
        is most minimal, distance computed as the square root of sum of squares
        of the seats of the parties.
        """
        bo_plot = bader_ofer.transpose(1,2,0)[day]
        bo_sqrsum = np.sqrt(np.sum((bo_plot[:,:,None] - bo_plot[:,None,:]) ** 2, axis=0)).mean(axis=0)
        return bader_ofer[bo_sqrsum.argmin()][day]
    
    def plot_mandates(self, samples, bader_ofer, max_bo=None, day=0, hebrew=True):
        """
        Plot the resulting mandates of the parties and their distributions.
        This is the bar graph most often seen in poll results.
        """
        
        from bidi import algorithm as bidialg
        
        fe=self.forecast_model
        parties = fe.parties
    
        bo_plot = bader_ofer.transpose(1,2,0)[day]
    
        if max_bo is None:
            max_bo = self.get_least_square_sum_seats(bader_ofer, day)

        num_passed_parties = len(np.where(max_bo > 0)[0])
        passed_parties = max_bo.argsort()[::-1]
        fig, plots = plt.subplots(2, num_passed_parties, figsize=(2 * num_passed_parties, 10), gridspec_kw={'height_ratios':[5,1]} )
        xlim_dists = []
        ylim_height = []
        max_bo_height = max_bo.max()
        for i in range(num_passed_parties) :
          party = passed_parties[i]
          name = bidialg.get_display(parties[fe.party_ids[party]]['hname']) if hebrew else parties[fe.party_ids[party]]['name']
          plots[0][i].set_title(name, va='bottom', y=-0.08, fontsize='large')
          mandates_count = np.unique(bo_plot[party], return_counts=True)
          mandates_bar = plots[0][i].bar([0], [max_bo[party]])[0] #, tick_label=[name])
          plots[0][i].set_xlim(-0.65,0.65)
          plots[0][i].text(mandates_bar.get_x() + mandates_bar.get_width()/2.0, mandates_bar.get_height(), '%d' % max_bo[party], ha='center', va='bottom', fontsize='x-large')
          plots[0][i].set_ylim(top=max_bo_height)
          bars = plots[1][i].bar(mandates_count[0], 100 * mandates_count[1] / len(bo_plot[party]))
          xticks = []
          max_start = 0
          if 0 in mandates_count[0]:
            xticks += [0]
            max_start = 1
            zero_rect = bars[0]
            plots[1][i].text(zero_rect.get_x() + zero_rect.get_width()/2.0, zero_rect.get_height(), ' %d%%' % (100 * mandates_count[1][0] / len(bo_plot[party])), ha='center', va='bottom')
          if len(mandates_count[1]) > max_start:
              max_index = max_start + np.argmax(mandates_count[1][max_start:])
              max_rect = bars[max_index]
              plots[1][i].text(max_rect.get_x() + max_rect.get_width()/2.0, max_rect.get_height(), ' %d%%' % (100 * mandates_count[1][max_index] / len(bo_plot[party])), ha='center', va='bottom')
              xticks += [mandates_count[0][max_index]]
          plots[1][i].set_xticks(xticks)
          xlim = plots[1][i].get_xlim()
          xlim_dists += [ xlim[1] - xlim[0] + 1 ]
          ylim_height += [ plots[1][i].get_ylim()[1] ]
          plots[0][i].grid(False)
          plots[0][i].tick_params(axis='both', which='both',left=False,bottom=False,labelbottom=False,labelleft=False)
          plots[0][i].set_facecolor('white')
          plots[1][i].grid(False)
          plots[1][i].tick_params(axis='y', which='both',left=False,labelleft=False)
          plots[1][i].set_facecolor('white')
        xlim_side = max(xlim_dists) / 2
        for i in range(num_passed_parties) :
          xlim = plots[1][i].get_xlim()
          xlim_center = (xlim[0] + xlim[1]) / 2
          plots[1][i].set_xlim(xlim_center - xlim_side, xlim_center + xlim_side)
          plots[1][i].set_ylim(top=max(ylim_height))
        bo_mean = bo_plot.mean(axis=1)
        failed_parties = [ i for i in bo_mean.argsort()[::-1] if max_bo[i] == 0 ]
        num_failed_parties = len(failed_parties)
        offset = num_passed_parties // 3
        failed_plots = []
        for failed_index in range(num_failed_parties):
            failed_plot = fig.add_subplot(num_failed_parties*2, num_passed_parties,
                num_passed_parties * (failed_index + 1) - offset, ymargin = 0.5)
            party = failed_parties[failed_index]
            mandates_count = np.unique(bo_plot[party], return_counts=True)
            if len(mandates_count[0]) > 1:
                max_start = 0
                xticks = []
                if 0 in mandates_count[0]:
                    xticks += [0]
                    max_start = 1
                    zero_rect = bars[0]
                    failed_plot.text(zero_rect.get_x() + zero_rect.get_width()/2.0, zero_rect.get_height(), ' %d%%' % (100 * mandates_count[1][0] / len(bo_plot[party])), ha='center', va='bottom')
                bars = failed_plot.bar(mandates_count[0], 100 * mandates_count[1] / len(bo_plot[party]))
                if len(mandates_count[1]) > max_start:
                    max_index = max_start + np.argmax(mandates_count[1][max_start:])
                    max_rect = bars[max_index]
                    failed_plot.text(max_rect.get_x() + max_rect.get_width()/2.0, max_rect.get_height(), ' %d%%' % (100 * mandates_count[1][max_index] / len(bo_plot[party])), ha='center', va='bottom')
                    xticks += [mandates_count[0][max_index]]
                failed_plot.set_xticks(xticks)
            else:
                failed_plot.text(0.8, 0.5, str(mandates_count[0][0]), ha='center', va='bottom')
            name = bidialg.get_display(parties[fe.party_ids[party]]['hname']) if hebrew else parties[fe.party_ids[party]]['name']
            failed_plot.set_ylabel(name, va='center', ha='right', rotation=0, fontsize='medium')
            failed_plot.yaxis.set_label_position("right")
            failed_plot.spines["right"].set_position(("axes", 1.25))
            failed_plot.grid(False)
            failed_plot.tick_params(axis='both', which='both',left=False,bottom=False,labelbottom=False,labelleft=False)
            failed_plot.set_facecolor('white')
            failed_plots += [ failed_plot ]
        if num_failed_parties > 0:
            max_failed_xlim = max([fp.get_xlim()[1] for fp in failed_plots])
            for fp in failed_plots:
                fp.set_xlim(right=max_failed_xlim)
                fp.set_ylim(0, 150)
                
        fig.text(.5, 1.05, bidialg.get_display('חלוקת המנדטים') if hebrew else 'Mandates Allocation', 
                 ha='center', fontsize='xx-large')
        fig.text(.5, .05, 'Generated using pyHoshen © 2019', ha='center')

    def plot_pollster_house_effects(self, samples, hebrew = True):
        """
        Plot the house effects of each pollster per party.
        """
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import matplotlib.ticker as ticker
        from bidi import algorithm as bidialg
        
        house_effects = samples.transpose(2,1,0)
        fe = self.forecast_model
        
        plots = []
        for i, party in enumerate(fe.party_ids):
          def pollster_label(pi, pollster_id):
              perc = '%.2f %%' % (100 * house_effects[i][pi].mean())
              if hebrew and len(fe.config['pollsters'][pollster_id]['hname']) > 0:
                  label = perc + ' :' + bidialg.get_display(fe.config['pollsters'][pollster_id]['hname'])
              else:
                  label = fe.config['pollsters'][pollster_id]['name'] + ': ' + perc
              return label
            
          cpalette = sns.color_palette("cubehelix", len(fe.pollster_ids))
          patches = [
              mpatches.Patch(color=cpalette[pi], label=pollster_label(pi, pollster))
              for pi, pollster in enumerate(fe.pollster_ids)]
    
          fig, ax = plt.subplots(figsize=(10, 2))
          legend = fig.legend(handles=patches, loc='best', ncol=2)
          if hebrew:
            for col in legend._legend_box._children[-1]._children:
                for c in col._children: 
                    c._children.reverse() 
                col.align="right" 
          ax.set_title(bidialg.get_display(fe.parties[party]['hname']) if hebrew 
                       else fe.parties[party]['name'])
          for pi, pollster_house_effects in enumerate(house_effects[i]):
            sns.kdeplot(100 * pollster_house_effects, shade=True, ax=ax, color=cpalette[pi])
          ax.xaxis.set_major_formatter(ticker.PercentFormatter(decimals=1))
          ax.yaxis.set_major_formatter(ticker.PercentFormatter(decimals=1))
          plots += [ax]
        fig.text(.5, 1.05, bidialg.get_display('הטיית הסוקרים') if hebrew else 'House Effects', 
                 ha='center', fontsize='xx-large')
        fig.text(.5, .05, 'Generated using pyHoshen © 2019', ha='center')

    def plot_party_support_evolution_graphs(self, samples, mbo = None, burn=None, hebrew = True):
        """
        Plot the evolving support of each party over time in both percentage and seats.
        """
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker
        import matplotlib.dates as mdates
        import datetime
        from bidi import algorithm as bidialg
    
        def get_dimensions(n):
            divisor = int(np.ceil(np.sqrt(n)))
            if n % divisor == 0:
                return n // divisor, divisor
            else:
                return 1 + n // divisor, divisor
     
        if burn is None:
            burn = -min(len(samples), 1000)
            
        if mbo is None:
            mbo = self.compute_trace_bader_ofer(samples['support'])
    
        samples = samples[burn:]
        mbo = mbo[burn:]
        
        fe = self.forecast_model
                
        mbo_by_party = np.transpose(mbo, [2, 1, 0])
        mandates = 0
        mbo_by_day_party = np.transpose(mbo, [1, 2, 0])
        party_avg = mbo_by_day_party[0].mean(axis=1).argsort()[::-1]
        date_list = [fe.forecast_day - datetime.timedelta(days=x) for x in range(0, fe.num_days)]
        dimensions = get_dimensions(fe.num_parties)
        fig, plots = plt.subplots(dimensions[1], dimensions[0], 
                                  figsize=(5.5 * dimensions[0], 3.5 * dimensions[1]))
        means = samples['support'].mean(axis=0)
        stds = samples['support'].std(axis=0)
    
        for index, party in enumerate(party_avg):
            party_config = fe.config['parties'][fe.party_ids[party]]
            
            if 'created' in party_config:
                days_to_show = (fe.forecast_day - datetime.datetime.strptime(party_config['created'], '%d/%m/%Y')).days
            else:
                days_to_show = fe.num_days

            vindex = index // dimensions[0]
            hindex = index % dimensions[0]
            if hebrew:
                hindex = -hindex - 1 # work right to left in hebrew
            subplots = plots[vindex]
            
            subplots[hindex].xaxis.set_major_formatter(mdates.DateFormatter('%d/%m'))
    
            title = bidialg.get_display(party_config['hname']) if hebrew else party_config['name']
            subplots[hindex].set_title(title)
    
            subplot = subplots[hindex].twinx()
    
            num_day_ticks = fe.num_days
            date_ticks = [fe.forecast_day - datetime.timedelta(days=x) 
                for x in range(0, num_day_ticks, 7)]
            subplots[hindex].set_xticks(date_ticks)
            subplots[hindex].set_xticklabels(subplots[hindex].get_xticklabels(), rotation=45)
            subplots[hindex].set_xlim(date_list[-1], date_list[0])
            subplot.set_xticks(date_ticks)
            subplot.set_xticklabels(subplot.get_xticklabels(), rotation=45)
            subplot.set_xlim(date_list[-1], date_list[0])
    
            party_means = means[:, party]
            party_std = stds[:, party]
            
            subplots[hindex].fill_between(date_list[:days_to_show],
                    100*party_means[:days_to_show] - 100*1.95996*party_std[:days_to_show],
                    100*party_means[:days_to_show] + 100*1.95996*party_std[:days_to_show],
                    color='#90ee90')
            subplots[hindex].plot(date_list[:days_to_show],
                    100*party_means[:days_to_show], color='#32cd32')
            if subplots[hindex].get_ylim()[0] < 0:
                subplots[hindex].set_ylim(bottom=0)
            subplots[hindex].yaxis.set_major_formatter(ticker.PercentFormatter(decimals=1))
            subplots[hindex].tick_params(axis='y', colors='#32cd32')
            subplots[hindex].yaxis.label.set_color('#32cd32')
                
            mand_means = np.mean(mbo_by_party[party], axis=1)
            mand_std = np.std(mbo_by_party[party], axis=1)
            subplot.fill_between(date_list[:days_to_show],
                    mand_means[:days_to_show] - 1.95996*mand_std[:days_to_show], 
                    mand_means[:days_to_show] + 1.95996*mand_std[:days_to_show], 
                    alpha=0.5, color='#6495ed')
            subplot.plot(date_list[:days_to_show], mand_means[:days_to_show], color='#4169e1')
            if subplot.get_ylim()[0] < 0:
                subplot.set_ylim(bottom=0)
            if subplot.get_ylim()[1] < 4:
                subplot.set_ylim(top=4)
            if int(subplot.get_ylim()[1]) == int(subplot.get_ylim()[0]):
                subplot.set_ylim(top=int(subplot.get_ylim()[0]) + 1)
            subplot.yaxis.set_major_locator(ticker.MaxNLocator(integer=True, min_n_ticks=2, prune=None))
            subplot.tick_params(axis='y', colors='#4169e1')
            subplot.yaxis.label.set_color('#4169e1')
                                          
            subplots[hindex].yaxis.tick_right()
            subplots[hindex].yaxis.set_label_position("right")
            subplots[hindex].spines["right"].set_position(("axes", 1.08))
            
            if hindex == dimensions[0] - 1:
                subplot.set_ylabel("Seats")
                subplots[hindex].set_ylabel("% Support")
                subplots[hindex].spines["right"].set_position(("axes", 1.13))
            elif hindex == -1:
                subplot.set_ylabel(bidialg.get_display("מנדטים"))
                subplots[hindex].set_ylabel(bidialg.get_display("אחוזי תמיכה"))
                subplots[hindex].spines["right"].set_position(("axes", 1.13))
     
            subplots[hindex].grid(False)           
            
            sup = [sample[party][0] for sample in samples['support']]
            mandates += int(120.0*sum(sup)/len(samples['support']))
            
            subplots[hindex].xaxis.set_major_formatter(mdates.DateFormatter('%d/%m'))
            
            subplots[hindex].set_xlim(date_ticks[-1], date_ticks[0])
            subplot.set_xlim(date_ticks[-1], date_ticks[0])
    
        for empty_subplot in range(len(party_avg), np.product(dimensions)):
            hindex = empty_subplot % dimensions[0]
            if hebrew:
                hindex = -hindex - 1 # work right to left in hebrew
            plots[-1][hindex].axis('off')
        plt.subplots_adjust(wspace=0.5,hspace=0.5)
    
        fig.text(.5, 1.05, bidialg.get_display('התמיכה במפלגות לאורך זמן') if hebrew
                 else 'Party Support over Time', 
                 ha='center', fontsize='xx-large')
        fig.text(.5, .05, 'Generated using pyHoshen © 2019', ha='center')

    def plot_correlation_matrix(self, correlation_matrix, hebrew=False):
        """
        Plot the given correlation matrix.
        """
        from bidi import algorithm as bidialg
        
        labels = [bidialg.get_display(v['hname']) if hebrew else v['name']
            for v in self.forecast_model.parties.values()]
    
        utils.plot_correlation_matrix(correlation_matrix, labels, alignRight=hebrew)

    def plot_election_correlation_matrices(self, correlation_matrices, hebrew=False):
        """
        Plot the distribution of correlation matrices.
        """
        from bidi import algorithm as bidialg
    
        labels = [bidialg.get_display(v['hname'])  if hebrew else v['name'] 
            for v in self.forecast_model.parties.values()]
        fig = utils.plot_correlation_matrices(correlation_matrices, labels, alignRight=hebrew)
        fig.text(.5, 1.05, bidialg.get_display('מטריצת המתאמים') if hebrew else 'Correlation Matrix', 
                 ha='center', fontsize='xx-large')
        fig.text(.5, .05, 'Generated using pyHoshen © 2019', ha='center')