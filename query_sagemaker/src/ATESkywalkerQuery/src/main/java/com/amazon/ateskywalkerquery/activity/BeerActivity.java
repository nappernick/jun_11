package com.amazon.ateskywalkerquery.activity;

import com.amazon.ateskywalkerquery.Beer;
import com.amazon.ateskywalkerquery.BeerList;
import com.amazon.ateskywalkerquery.DependencyException;
import com.amazon.ateskywalkerquery.ICreateBeerActivity;
import com.amazon.ateskywalkerquery.IGetAllBeersActivity;
import com.amazon.coral.annotation.Service;
import com.amazon.coral.service.Activity;
import com.amazon.coral.service.LogRequests;
import com.amazon.coral.validate.ValidationException;

import java.util.List;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Activity class for all Beer-related methods.
 */
@Service("ATESkywalkerQuery")
public class BeerActivity extends Activity implements IGetAllBeersActivity, ICreateBeerActivity {

    private static final Set<Beer> SAVED_BEERS = ConcurrentHashMap.newKeySet();
    private static final AtomicLong NEXT_BEER_ID = new AtomicLong(1);

    /**
     * List all beers in the service.
     *
     * @return A list of beers
     * @throws DependencyException On any dependency error
     */
    /*
     * @LogRequests annotation is an opt-in mechanism and will log the
     * input and output parameters from your Coral model. If any of
     * these values are sensitive or confidential, you can either
     * remove the @LogRequests annotation or mark the data as sensitive:
     * https://w.amazon.com/bin/view/Coral/Model/XML/Traits#Sensitive
     */
    @Override
    @LogRequests
    public BeerList getAllBeers() throws DependencyException {
        BeerList beerList = new BeerList();
        beerList.setBeers(List.copyOf(SAVED_BEERS));
        return beerList;
    }

    /**
     * Given a beer object (without id) create a new Beer in the system.
     *
     * @param inBeer The beer information (without id) to populate
     * @return The created beer (with id)
     * @throws Exception On any error
     */
    @Override
    /*
     * @LogRequests annotation is an opt-in mechanism and will log the
     * input and output parameters from your Coral model. If any of
     * these values are sensitive or confidential, you can either
     * remove the @LogRequests annotation or mark the data as sensitive:
     * https://w.amazon.com/bin/view/Coral/Model/XML/Traits#Sensitive
     */
    @LogRequests
    public Beer createBeer(Beer inBeer) {
        // Check the name
        if (inBeer.getName() == null || inBeer.getName().isEmpty()) {
            throw new ValidationException("Invalid beer name!");
        }
        // It's very bad practice to mutate the input object, and can cause
        // lots of confusion. Making a copy below.
        Beer beerWithAssignedId = new Beer.Builder()
            .withBeerId(NEXT_BEER_ID.getAndIncrement())
            .withName(inBeer.getName())
            .withDescription(inBeer.getDescription())
            .withBeerCompanyName(inBeer.getBeerCompanyName())
            .withBeerTypeName(inBeer.getBeerTypeName())
            .build();
        SAVED_BEERS.add(beerWithAssignedId);
        return beerWithAssignedId;
    }

    /**
     * Clear out all BeerModels stored in the database.
     * Used for testing.
     */
    static void clearBeers() {
        /*
         * This is not truly safe, and does not always work in multithreaded scenarios.
         * It's acceptable here since this is only used for testing.
         *
         * If this was exposed (as an Operation perhaps), then a createBeer call could happen between
         * the call to clear() and the setId(), which would result in a beer in savedBeers existing with a
         * after this call returns.
         */
        SAVED_BEERS.clear();
        NEXT_BEER_ID.set(1);
    }
}
