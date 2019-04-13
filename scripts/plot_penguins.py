from scripts.make_figures import *

figure_path = "/home/oliver/sync/figures/aerial_penguins"

if __name__ == '__main__':

    loaded_a = load_all(penguins_a, base_path)
    loaded_b = load_all(penguins_b, base_path)

    pprint_struct(pluck_struct('summary', loaded_a))
    pprint_struct(pluck_struct('summary', loaded_b))

    # oliver = load_all(penguins_oliver, base_path)
    # dad = load_all(penguins_dad, base_path)

    fig, ax = cumulative_instances( loaded_a._merge(loaded_b) )
    fig.savefig(path.join(figure_path, "cum_instances.pdf"), bbox_inches='tight')



    fig, ax = actions_time(loaded_b)
    ax.set_ylim(ymin=0, ymax=150)
    ax.set_xlim(xmin=0, xmax=50)

    fig.savefig(path.join(figure_path, "actions_time_b.pdf"), bbox_inches='tight')

    fig, ax = actions_time(loaded_a)
    ax.set_ylim(ymin=0, ymax=150)
    ax.set_xlim(xmin=0, xmax=50)

    fig.savefig(path.join(figure_path, "actions_time_a.pdf"), bbox_inches='tight')